"""CAS-guarded snapshot import apply.

Disposable tmp graphs and export dirs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_import_apply.py -q
"""
from __future__ import annotations

import ast
import errno
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
    STAGING_WRITER_LOCK_NAME,
    probe_staging_writer_lease,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_import_apply import (  # type: ignore
    HASH_CHUNK_BYTES,
    SnapshotImportApplyError,
    SnapshotImportApplyIntegrityError,
    format_result,
    result_to_json,
    snapshot_import_apply,
)
from graphrag_code.snapshot_import_plan import snapshot_import_plan  # type: ignore
from graphrag_code.snapshot_export_plan import (  # type: ignore
    ACCEPTED_PAYLOAD_FILES,
    snapshot_export_plan,
)
from graphrag_code.snapshot_pins import (  # type: ignore
    OPERATOR_PINS_NAME,
    snapshot_pin,
    snapshot_pins_list,
)

SCRIPT = ROOT / "scripts" / "snapshot_import_apply.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_import_apply.py"
NATIVE = ROOT / "src" / "graphrag_code" / "_rename_noreplace.py"
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
        "snapshot_history",
        "snapshot_diff",
        "retained_snapshot_read",
        "audit_graph_root",
        "resolve_snapshot",
        "doctor_fingerprint",
        "validate_persisted_graph_integrity",
        "c_clang_ast_capture",
        "c_compiler_facts",
        "_publication_lock",
        "rmtree",
        "read_bytes",
    }
)
FORBIDDEN_WORDS = ("orphaned", "abandoned", "expired", "safe to delete")
REQUIRED_RESULT_KEYS = (
    "schema_version",
    "ok",
    "graph",
    "export_directory",
    "snapshot_id",
    "expected_import_revision",
    "observed_import_revision",
    "source_export_revision",
    "planned_files",
    "file_count",
    "total_size_bytes",
    "import_confirmed",
    "import_performed",
    "publication_attempted",
    "publication_performed",
    "snapshot_verified_after_publication",
    "current_before",
    "current_after",
    "current_unchanged",
    "staging_created",
    "staging_cleanup_attempted",
    "staging_remaining",
    "snapshots_fsync_confirmed",
    "partial",
    "filesystem_may_have_changed",
    "retry_requires_fresh_plan",
    "graph_mutated",
    "export_mutated",
    "activation_performed",
    "retention_performed",
    "error",
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
        settings_text=f"import-apply: {marker}\n",
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


def _staging_leftovers(graph: Path) -> list[Path]:
    snaps = graph / "snapshots"
    if not snaps.is_dir():
        return []
    return sorted(
        path
        for path in snaps.iterdir()
        if path.name.startswith(STAGING_NAME_PREFIX)
    )


def _file_revision(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), "sha256:" + hashlib.sha256(data).hexdigest()


def _prepare(tmp_path: Path, *, observations: bool = False):
    source = tmp_path / "source"
    target = tmp_path / "target"
    live = _publish(source, "src", observations=observations)
    dest_live = _publish(target, "dst")
    export_dir = _copy_standalone(live, tmp_path / "export")
    plan = snapshot_import_plan(target, export_dir)
    return source, target, live, dest_live, export_dir, plan


def _write_witness(directory_fd: int, name: str, payload: bytes = b"witness") -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)


def _replace_named_directory(parent_fd: int, name: str, witness: bytes = b"witness") -> None:
    aside = name + ".aside"
    os.rename(name, aside, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    new_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        _write_witness(new_fd, "witness.bin", witness)
    finally:
        os.close(new_fd)


def _assert_success_shape(
    result: dict,
    graph: Path,
    export_dir: Path,
    snapshot_id: str,
    expected: str,
) -> None:
    for key in REQUIRED_RESULT_KEYS:
        assert key in result
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["graph"] == str(graph.resolve())
    assert result["export_directory"] == str(export_dir.resolve())
    assert result["snapshot_id"] == snapshot_id
    assert result["expected_import_revision"] == expected
    assert result["observed_import_revision"] == expected
    assert result["import_confirmed"] is True
    assert result["import_performed"] is True
    assert result["publication_attempted"] is True
    assert result["publication_performed"] is True
    assert result["snapshot_verified_after_publication"] is True
    assert result["current_before"] == result["current_after"]
    assert result["current_unchanged"] is True
    assert result["staging_created"] is True
    assert result["staging_remaining"] is False
    assert result["snapshots_fsync_confirmed"] is True
    assert result["partial"] is False
    assert result["filesystem_may_have_changed"] is True
    assert result["retry_requires_fresh_plan"] is False
    assert result["graph_mutated"] is True
    assert result["export_mutated"] is False
    assert result["activation_performed"] is False
    assert result["retention_performed"] is False
    assert result["error"] is None
    assert result["file_count"] == len(result["planned_files"])
    assert result["total_size_bytes"] == sum(
        item["size_bytes"] for item in result["planned_files"]
    )
    published = graph / "snapshots" / snapshot_id
    assert published.is_dir() and not published.is_symlink()
    assert not (graph / "snapshots" / f"{STAGING_NAME_PREFIX}{snapshot_id}").exists()
    for item in result["planned_files"]:
        src_size, src_rev = _file_revision(export_dir / item["path"])
        dst_size, dst_rev = _file_revision(published / item["path"])
        assert item["size_bytes"] == src_size == dst_size
        assert item["content_revision"] == src_rev == dst_rev
    assert json.loads((published / "manifest.json").read_text(encoding="utf-8"))["id"] == snapshot_id
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == [
        "import_is_not_backup",
        "import_revision_is_cas_only",
        "import_is_not_activation",
        "crash_may_leave_private_staging",
        "staging_writer_lease_not_ownership",
        "advisory_locks_cooperating_only",
        "source_envelope_language_independent_only",
        "cli_only_not_mcp",
    ]
    text = format_result(result)
    assert "import_performed=true" in text
    assert "current_unchanged=true" in text
    assert "not a backup" in text
    assert "not an activation" in text
    json_text = result_to_json(result)
    assert json.loads(json_text) == result


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
    plan = snapshot_import_plan(target, export_dir)
    shared = [
        "--graph",
        "target",
        "--export-dir",
        "export",
        "--expected-import-revision",
        plan["import_revision"],
        "--import-confirmed",
        "--json",
    ]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_import_apply", *shared],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == 0, module.stderr
    body = json.loads(module.stdout)
    assert body["snapshot_id"] == live.name
    assert result_to_json(body) == module.stdout

    source2 = here / "source2"
    target2 = here / "target2"
    live2 = _publish(source2, "src2")
    _publish(target2, "dst2")
    export2 = _copy_standalone(live2, here / "export2")
    plan2 = snapshot_import_plan(target2, export2)
    script = _run(
        "--graph",
        "target2",
        "--export-dir",
        "export2",
        "--expected-import-revision",
        plan2["import_revision"],
        "--import-confirmed",
        "--json",
        cwd=here,
    )
    cli_source = here / "source3"
    cli_target = here / "target3"
    live3 = _publish(cli_source, "src3")
    _publish(cli_target, "dst3")
    export3 = _copy_standalone(live3, here / "export3")
    plan3 = snapshot_import_plan(cli_target, export3)
    cli = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "snapshot-import-apply",
            "--graph",
            "target3",
            "--export-dir",
            "export3",
            "--expected-import-revision",
            plan3["import_revision"],
            "--import-confirmed",
            "--json",
        ],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert script.returncode == cli.returncode == 0, (script.stderr, cli.stderr)
    assert json.loads(script.stdout)["ok"] is True
    assert json.loads(cli.stdout)["ok"] is True

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    wheel_source = outside / "wsrc"
    wheel_target = outside / "wdst"
    wheel_live = _publish(wheel_source, "wheel")
    _publish(wheel_target, "wdst")
    wheel_export = _copy_standalone(wheel_live, outside / "wexport")
    wheel_plan = snapshot_import_plan(wheel_target, wheel_export)
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-import-apply",
            "--graph",
            str(wheel_target),
            "--export-dir",
            str(wheel_export),
            "--expected-import-revision",
            wheel_plan["import_revision"],
            "--import-confirmed",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["ok"] is True

    help_proc = subprocess.run(
        [sys.executable, str(CLI), "snapshot-import-apply", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert help_proc.returncode == 0
    assert "--import-confirmed" in help_proc.stdout
    assert "--expected-import-revision" in help_proc.stdout

    import tarfile

    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_import_apply.py" in names
    assert "_rename_noreplace.py" in names


def test_successful_import_preserves_bytes_current_and_pins(tmp_path: Path):
    _source, target, live, dest_live, export_dir, plan = _prepare(
        tmp_path, observations=True
    )
    export_fp = _root_fingerprint(export_dir)
    before = _protected_state(target)
    pins_before = snapshot_pins_list(target)
    pin_result = snapshot_pin(
        target, dest_live.name, pins_before["registry_revision"], pin_confirmed=True
    )
    before_pinned = _protected_state(target)
    result = snapshot_import_apply(
        target,
        export_dir,
        plan["import_revision"],
        import_confirmed=True,
    )
    _assert_success_shape(result, target, export_dir, live.name, plan["import_revision"])
    assert _current(target) == dest_live.name
    assert result["current_before"] == dest_live.name
    published = target / "snapshots" / live.name
    assert published.is_dir()
    for name in ACCEPTED_PAYLOAD_FILES:
        src = export_dir / name
        dst = published / name
        if src.exists():
            assert dst.read_bytes() == src.read_bytes()
    assert json.loads((published / "manifest.json").read_text(encoding="utf-8"))["id"] == live.name
    assert _root_fingerprint(export_dir) == export_fp
    after = _protected_state(target)
    assert after["current"] == before_pinned["current"]
    assert after["registry"] == before_pinned["registry"]
    assert after["lock"] == before_pinned["lock"]
    assert after["lock_stat"] == before_pinned["lock_stat"]
    assert set(after["listing"]) == set(before_pinned["listing"]) | {live.name}
    for rel, digest in before_pinned["hashes"].items():
        assert after["hashes"][rel] == digest
    pins_after = snapshot_pins_list(target)
    assert pins_after["registry_revision"] == pin_result["registry_revision"]
    assert dest_live.name in pins_after["operator_pins"]
    assert live.name not in pins_after["operator_pins"]
    assert pins_after["claim_pins"] == pins_before["claim_pins"]
    assert _staging_leftovers(target) == []
    later_plan = snapshot_import_plan(target, export_dir)
    assert later_plan["import_ready"] is False
    assert "snapshot_id_already_published" in later_plan["blocking_reasons"]
    export_plan = snapshot_export_plan(target, dest_live.name)
    assert export_plan["ok"] is True
    assert export_plan["export_performed"] is False


def test_cas_mismatch_and_blocked_plan_create_nothing(tmp_path: Path):
    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    before = _protected_state(target)
    export_fp = _root_fingerprint(export_dir)
    stale = "sha256:" + ("ab" * 32)
    with pytest.raises(SnapshotImportApplyIntegrityError, match="no longer matches"):
        snapshot_import_apply(target, export_dir, stale, import_confirmed=True)
    assert _protected_state(target) == before
    assert _root_fingerprint(export_dir) == export_fp
    assert _staging_leftovers(target) == []

    proc = _run(
        "--graph",
        str(target),
        "--export-dir",
        str(export_dir),
        "--expected-import-revision",
        stale,
        "--import-confirmed",
        "--json",
    )
    assert proc.returncode == 1
    assert proc.stdout == ""

    collision = tmp_path / "collision"
    _publish(collision, "dst")
    shutil.copytree(live, collision / "snapshots" / live.name)
    blocked = snapshot_import_plan(collision, export_dir)
    assert blocked["import_ready"] is False
    with pytest.raises(SnapshotImportApplyIntegrityError, match="blocked|already"):
        snapshot_import_apply(
            collision, export_dir, blocked["import_revision"], import_confirmed=True
        )
    assert not (collision / "snapshots" / f"{STAGING_NAME_PREFIX}{live.name}").exists()

    staging_graph = tmp_path / "staging-block"
    _publish(staging_graph, "dst")
    (staging_graph / "snapshots" / f"{STAGING_NAME_PREFIX}{live.name}").mkdir()
    staged = snapshot_import_plan(staging_graph, export_dir)
    assert staged["target_staging_present"] is True
    with pytest.raises(SnapshotImportApplyIntegrityError, match="blocked|already"):
        snapshot_import_apply(
            staging_graph, export_dir, staged["import_revision"], import_confirmed=True
        )


def test_final_id_and_exact_staging_name_collision(tmp_path: Path):
    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    (target / "snapshots" / live.name).mkdir()
    with pytest.raises(
        SnapshotImportApplyIntegrityError, match="already|present|blocked|no longer matches"
    ):
        snapshot_import_apply(
            target, export_dir, plan["import_revision"], import_confirmed=True
        )

    other = tmp_path / "stage-name"
    _publish(other, "dst")
    export2 = _copy_standalone(live, tmp_path / "export2")
    plan2 = snapshot_import_plan(other, export2)
    (other / "snapshots" / f"{STAGING_NAME_PREFIX}{live.name}").mkdir()
    with pytest.raises(
        SnapshotImportApplyIntegrityError, match="already|present|blocked|no longer matches"
    ):
        snapshot_import_apply(
            other, export2, plan2["import_revision"], import_confirmed=True
        )


def test_atomic_race_before_publication_does_not_replace(tmp_path: Path, monkeypatch):
    import graphrag_code.snapshot_import_apply as apply_mod

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)

    def plant_empty(_snapshots_fd, snapshot_id, _staging_name):
        os.mkdir(snapshot_id, 0o700, dir_fd=_snapshots_fd)

    monkeypatch.setattr(apply_mod, "_before_import_apply_publication", plant_empty)
    result = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["publication_attempted"] is True
    assert result["publication_performed"] is False
    published = target / "snapshots" / live.name
    assert published.is_dir()
    assert list(published.iterdir()) == []
    leftovers = _staging_leftovers(target)
    if leftovers:
        for path in leftovers:
            shutil.rmtree(path)


def test_missing_confirmation_and_malformed_revision_create_nothing(tmp_path: Path):
    _source, target, _live, _dest, export_dir, plan = _prepare(tmp_path)
    before = _protected_state(target)
    with pytest.raises(SnapshotImportApplyError, match="import-confirmed"):
        snapshot_import_apply(
            target, export_dir, plan["import_revision"], import_confirmed=False
        )
    proc = _run(
        "--graph",
        str(target),
        "--export-dir",
        str(export_dir),
        "--expected-import-revision",
        plan["import_revision"],
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert _protected_state(target) == before
    with pytest.raises(SnapshotImportApplyError, match="sha256"):
        snapshot_import_apply(
            target, export_dir, "SHA256:" + ("ab" * 32), import_confirmed=True
        )
    with pytest.raises(SnapshotImportApplyError, match="whitespace"):
        snapshot_import_apply(
            target,
            export_dir,
            " " + plan["import_revision"],
            import_confirmed=True,
        )
    bad = _run(
        "--graph",
        str(target),
        "--export-dir",
        str(export_dir),
        "--expected-import-revision",
        "sha256:not-a-digest",
        "--import-confirmed",
        "--json",
    )
    assert bad.returncode == 2
    assert bad.stdout == ""
    assert _protected_state(target) == before
    assert _staging_leftovers(target) == []


def test_unsupported_noreplace_creates_nothing(tmp_path: Path, monkeypatch):
    import graphrag_code.snapshot_import_apply as apply_mod

    _source, target, _live, _dest, export_dir, plan = _prepare(tmp_path)
    before = _protected_state(target)
    monkeypatch.setattr(apply_mod, "rename_noreplace_supported", lambda: False)
    with pytest.raises(SnapshotImportApplyError, match="unsupported"):
        snapshot_import_apply(
            target, export_dir, plan["import_revision"], import_confirmed=True
        )
    assert _protected_state(target) == before
    assert _staging_leftovers(target) == []


def test_source_changes_before_during_after_copy_and_before_publication(
    tmp_path: Path, monkeypatch
):
    import graphrag_code.snapshot_import_apply as apply_mod

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    payload = export_dir / "entities.parquet"
    original = payload.read_bytes()
    original_stat = payload.stat()

    def rewrite(_root, _records):
        replacement = bytes([original[0] ^ 1]) + original[1:]
        payload.write_bytes(replacement)
        os.utime(payload, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    monkeypatch.setattr(apply_mod, "_before_import_apply_copy", rewrite)
    result = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["retry_requires_fresh_plan"] is True
    leftovers = _staging_leftovers(target)
    for path in leftovers:
        shutil.rmtree(path)
    payload.write_bytes(original)

    _source2, target2, _live2, _dest2, export2, plan2 = _prepare(tmp_path / "during")
    during_payload = export2 / "entities.parquet"
    during_original = during_payload.read_bytes()
    reads = {"n": 0}
    original_read = apply_mod._read_chunk

    def mutate_during(fd, size):
        reads["n"] += 1
        if reads["n"] == 2:
            during_payload.write_bytes(b"X" + during_original[1:])
        return original_read(fd, size)

    monkeypatch.setattr(apply_mod, "_before_import_apply_copy", lambda *_: None)
    monkeypatch.setattr(apply_mod, "_read_chunk", mutate_during)
    result = snapshot_import_apply(
        target2, export2, plan2["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target2):
        shutil.rmtree(path)
    monkeypatch.setattr(apply_mod, "_read_chunk", original_read)

    _source3, target3, _live3, _dest3, export3, plan3 = _prepare(tmp_path / "after")
    after_payload = export3 / "entities.parquet"
    after_original = after_payload.read_bytes()
    after_stat = after_payload.stat()

    def rewrite_after(_root, _records):
        replacement = bytes([after_original[0] ^ 1]) + after_original[1:]
        after_payload.write_bytes(replacement)
        os.utime(
            after_payload, ns=(after_stat.st_atime_ns, after_stat.st_mtime_ns)
        )

    monkeypatch.setattr(apply_mod, "_after_import_apply_copied", rewrite_after)
    result = snapshot_import_apply(
        target3, export3, plan3["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target3):
        shutil.rmtree(path)

    _source4, target4, live4, _dest4, export4, plan4 = _prepare(tmp_path / "before-pub")
    pub_payload = export4 / "entities.parquet"
    pub_original = pub_payload.read_bytes()
    pub_stat = pub_payload.stat()

    def rewrite_before_pub(_snapshots_fd, _snapshot_id, _staging_name):
        replacement = bytes([pub_original[0] ^ 1]) + pub_original[1:]
        pub_payload.write_bytes(replacement)
        os.utime(pub_payload, ns=(pub_stat.st_atime_ns, pub_stat.st_mtime_ns))

    monkeypatch.setattr(apply_mod, "_after_import_apply_copied", lambda *_: None)
    monkeypatch.setattr(
        apply_mod, "_before_import_apply_publication", rewrite_before_pub
    )
    result = snapshot_import_apply(
        target4, export4, plan4["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["publication_performed"] is False
    assert not (target4 / "snapshots" / live4.name).exists()
    for path in _staging_leftovers(target4):
        shutil.rmtree(path)

    post_case = tmp_path / "post-pub-source"
    _source5, target5, live5, _dest5, export5, plan5 = _prepare(post_case)
    post_payload = export5 / "entities.parquet"
    post_original = post_payload.read_bytes()
    post_stat = post_payload.stat()

    def rewrite_after_target(_root, _snapshot_id):
        replacement = bytes([post_original[0] ^ 1]) + post_original[1:]
        post_payload.write_bytes(replacement)
        os.utime(
            post_payload, ns=(post_stat.st_atime_ns, post_stat.st_mtime_ns)
        )

    monkeypatch.setattr(
        apply_mod,
        "_before_import_apply_publication",
        lambda *_: None,
    )
    monkeypatch.setattr(
        apply_mod,
        "_after_import_apply_post_publication_target_observed",
        rewrite_after_target,
    )
    result = snapshot_import_apply(
        target5, export5, plan5["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["publication_performed"] is True
    assert result["snapshot_verified_after_publication"] is True
    assert (target5 / "snapshots" / live5.name).is_dir()


def test_short_io_hash_and_fsync_failures(tmp_path: Path, monkeypatch):
    import graphrag_code.snapshot_import_apply as apply_mod

    _source, target, _live, _dest, export_dir, plan = _prepare(tmp_path)
    original_read = apply_mod._read_chunk

    def one_byte(fd, size):
        return original_read(fd, 1 if size > 1 else size)

    monkeypatch.setattr(apply_mod, "_read_chunk", one_byte)
    result = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    assert result["ok"] is True
    monkeypatch.setattr(apply_mod, "_read_chunk", original_read)

    fail = tmp_path / "fail"
    _source_f, target_f, _live_f, _dest_f, export_f, plan_f = _prepare(fail)

    def truncated(_fd, _size):
        return b""

    monkeypatch.setattr(apply_mod, "_read_chunk", truncated)
    result = snapshot_import_apply(
        target_f, export_f, plan_f["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target_f):
        shutil.rmtree(path)
    monkeypatch.setattr(apply_mod, "_read_chunk", original_read)

    def boom_write(_fd, _data):
        raise OSError(errno.EIO, "injected write failure")

    write_case = tmp_path / "write"
    _s, target_w, _l, _d, export_w, plan_w = _prepare(write_case)
    monkeypatch.setattr(apply_mod, "_write_chunk", boom_write)
    result = snapshot_import_apply(
        target_w, export_w, plan_w["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target_w):
        shutil.rmtree(path)
    monkeypatch.setattr(apply_mod, "_write_chunk", apply_mod.os.write)

    def boom_file_fsync(_fd):
        raise apply_mod.SnapshotImportApplyError("fsync import payload failed: injected")

    file_case = tmp_path / "file-fsync"
    _s, target_ff, _l, _d, export_ff, plan_ff = _prepare(file_case)
    monkeypatch.setattr(apply_mod, "_fsync_file", boom_file_fsync)
    result = snapshot_import_apply(
        target_ff, export_ff, plan_ff["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target_ff):
        shutil.rmtree(path)

    def real_file_fsync(fd):
        apply_mod._fsync(fd)

    monkeypatch.setattr(apply_mod, "_fsync_file", real_file_fsync)

    def boom_staging_fsync(_fd):
        raise apply_mod.SnapshotImportApplyError(
            "fsync import staging directory failed: injected"
        )

    stage_case = tmp_path / "stage-fsync"
    _s, target_sf, _l, _d, export_sf, plan_sf = _prepare(stage_case)
    monkeypatch.setattr(apply_mod, "_fsync_staging_directory", boom_staging_fsync)
    result = snapshot_import_apply(
        target_sf, export_sf, plan_sf["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["publication_performed"] is False
    for path in _staging_leftovers(target_sf):
        shutil.rmtree(path)

    def real_staging_fsync(fd):
        apply_mod._fsync(fd)

    monkeypatch.setattr(apply_mod, "_fsync_staging_directory", real_staging_fsync)

    def boom_snapshots_fsync(_fd):
        raise apply_mod.SnapshotImportApplyError(
            "fsync snapshots directory failed: injected"
        )

    snap_case = tmp_path / "snap-fsync"
    _s, target_sn, live_sn, _d, export_sn, plan_sn = _prepare(snap_case)
    monkeypatch.setattr(apply_mod, "_fsync_snapshots_directory", boom_snapshots_fsync)
    result = snapshot_import_apply(
        target_sn, export_sn, plan_sn["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["publication_performed"] is True
    assert result["import_performed"] is True
    assert result["snapshots_fsync_confirmed"] is False
    assert (target_sn / "snapshots" / live_sn.name).is_dir()


def test_staged_payload_corruption_and_replacements(tmp_path: Path, monkeypatch):
    import graphrag_code.snapshot_import_apply as apply_mod

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    holder: dict[str, Path] = {}

    def remember(_snapshots_fd, staging_name, _staging_fd):
        holder["path"] = target / "snapshots" / staging_name

    def corrupt(_root, _records):
        payload = holder["path"] / "entities.parquet"
        payload.write_bytes(payload.read_bytes() + b"X")

    monkeypatch.setattr(apply_mod, "_after_import_apply_staging_opened", remember)
    monkeypatch.setattr(apply_mod, "_after_import_apply_copied", corrupt)
    result = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target):
        shutil.rmtree(path)

    link_case = tmp_path / "symlink"
    _s, target_l, _l, _d, export_l, plan_l = _prepare(link_case)

    def plant_symlink(_root, _records):
        staging = _staging_leftovers(target_l)[0]
        payload = staging / "relationships.parquet"
        payload.unlink()
        payload.symlink_to(export_l / "relationships.parquet")

    monkeypatch.setattr(apply_mod, "_after_import_apply_staging_opened", lambda *_: None)
    monkeypatch.setattr(apply_mod, "_after_import_apply_copied", plant_symlink)
    result = snapshot_import_apply(
        target_l, export_l, plan_l["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target_l):
        shutil.rmtree(path)

    hard_case = tmp_path / "hardlink"
    _s, target_h, _l, _d, export_h, plan_h = _prepare(hard_case)

    def plant_hardlink(_root, _records):
        staging = _staging_leftovers(target_h)[0]
        payload = staging / "text_units.parquet"
        other = staging / "alias.parquet"
        os.link(payload, other)

    monkeypatch.setattr(apply_mod, "_after_import_apply_copied", plant_hardlink)
    result = snapshot_import_apply(
        target_h, export_h, plan_h["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target_h):
        shutil.rmtree(path)

    fifo_case = tmp_path / "fifo"
    _s, target_f, _l, _d, export_f, plan_f = _prepare(fifo_case)

    def plant_fifo(_root, _records):
        staging = _staging_leftovers(target_f)[0]
        payload = staging / "settings.yaml"
        payload.unlink()
        os.mkfifo(payload)

    monkeypatch.setattr(apply_mod, "_after_import_apply_copied", plant_fifo)
    result = snapshot_import_apply(
        target_f, export_f, plan_f["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target_f):
        if path.is_dir() and not path.is_symlink():
            for child in path.iterdir():
                if child.is_fifo() or child.is_file() or child.is_symlink():
                    child.unlink()
            path.rmdir()

    manifest_case = tmp_path / "manifest"
    _s, target_m, _l, _d, export_m, plan_m = _prepare(manifest_case)

    def corrupt_manifest(_root, _records):
        staging = _staging_leftovers(target_m)[0]
        path = staging / "manifest.json"
        parsed = json.loads(path.read_text(encoding="utf-8"))
        parsed["counts"]["entities"] = int(parsed["counts"]["entities"]) + 1
        path.write_text(json.dumps(parsed), encoding="utf-8")

    monkeypatch.setattr(apply_mod, "_after_import_apply_copied", corrupt_manifest)
    result = snapshot_import_apply(
        target_m, export_m, plan_m["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target_m):
        shutil.rmtree(path)

    parquet_case = tmp_path / "parquet"
    _s, target_p, _l, _d, export_p, plan_p = _prepare(parquet_case)

    def corrupt_parquet(_root, _records):
        staging = _staging_leftovers(target_p)[0]
        (staging / "entities.parquet").write_bytes(b"not-parquet")

    monkeypatch.setattr(apply_mod, "_after_import_apply_copied", corrupt_parquet)
    result = snapshot_import_apply(
        target_p, export_p, plan_p["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    for path in _staging_leftovers(target_p):
        shutil.rmtree(path)


def test_writer_lease_held_and_cleanup_contention(tmp_path: Path, monkeypatch):
    import graphrag_code.snapshot_import_apply as apply_mod
    from graphrag_code.snapshot_staging_cleanup_plan import (  # type: ignore
        snapshot_staging_cleanup_plan,
    )

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    seen = {"held": False, "candidate": True}

    def after_copy(_root, _records):
        leftovers = _staging_leftovers(target)
        assert leftovers
        probe = probe_staging_writer_lease(leftovers[0])
        seen["held"] = probe["writer_lease_state"] == "held_by_cooperating_writer"
        cleanup_plan = snapshot_staging_cleanup_plan(target)
        names = [
            item.get("name")
            for item in (cleanup_plan.get("deletion_candidates") or [])
        ]
        seen["candidate"] = f"{STAGING_NAME_PREFIX}{live.name}" in names

    monkeypatch.setattr(apply_mod, "_after_import_apply_copied", after_copy)
    result = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    assert result["ok"] is True
    assert seen["held"] is True
    assert seen["candidate"] is False


def test_writer_lease_held_while_waiting_for_exclusive_lease(
    tmp_path: Path, monkeypatch
):
    import graphrag_code.byog_graph as byog
    import graphrag_code.snapshot_import_apply as apply_mod

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    held = threading.Event()
    release = threading.Event()
    seen = {"held_before_exclusive": False}

    def holder():
        with byog.graph_exclusive_lease(target):
            held.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(timeout=5)

    def before_exclusive(_root, staging_name):
        probe = probe_staging_writer_lease(target / "snapshots" / staging_name)
        seen["held_before_exclusive"] = (
            probe["writer_lease_state"] == "held_by_cooperating_writer"
        )
        release.set()

    monkeypatch.setattr(
        apply_mod, "_before_import_apply_exclusive_lease", before_exclusive
    )
    result = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    thread.join(timeout=5)
    assert result["ok"] is True
    assert seen["held_before_exclusive"] is True
    assert (target / "snapshots" / live.name).is_dir()


def test_current_published_and_lock_changes_between_plan_and_publication(
    tmp_path: Path, monkeypatch
):
    import graphrag_code.snapshot_import_apply as apply_mod

    _source, target, _live, dest_live, export_dir, plan = _prepare(tmp_path)
    extra = _publish(tmp_path / "extra-src", "extra")

    def change_current(_root, _staging_name):
        (target / "current").write_text(dest_live.name + "\n", encoding="utf-8")
        shutil.copytree(extra, target / "snapshots" / extra.name)

    monkeypatch.setattr(
        apply_mod, "_after_import_apply_exclusive_lease", change_current
    )
    result = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["publication_performed"] is False
    assert result["current_after"] is None
    assert result["current_unchanged"] is False
    for path in _staging_leftovers(target):
        shutil.rmtree(path)


def test_path_symlink_and_directory_replacement_races(tmp_path: Path, monkeypatch):
    import graphrag_code.snapshot_import_apply as apply_mod
    import graphrag_code.snapshot_import_plan as import_mod

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)
    replacement = tmp_path / "replacement-export"
    _copy_standalone(live, replacement)
    (replacement / "entities.parquet").write_bytes(b"replacement-bytes")
    hidden = tmp_path / "hidden-export"

    def replace_export(path):
        if path == export_dir or path.resolve() == export_dir.resolve():
            export_dir.rename(hidden)
            export_dir.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(import_mod, "_after_import_source_path_inspected", replace_export)
    with pytest.raises(
        (SnapshotImportApplyIntegrityError, SnapshotImportApplyError),
        match="changed|replaced|symlink",
    ):
        snapshot_import_apply(
            target, export_dir, plan["import_revision"], import_confirmed=True
        )
    assert (replacement / "entities.parquet").read_bytes() == b"replacement-bytes"

    graph_case = tmp_path / "graph-link"
    _s, target_g, _l, _d, export_g, plan_g = _prepare(graph_case)
    raced_real = tmp_path / "raced-real-graph"
    _publish(raced_real, "raced")
    hidden_real = tmp_path / "hidden-real-graph"

    def replace_graph(path):
        if path == raced_real:
            raced_real.rename(hidden_real)
            raced_real.symlink_to(target_g, target_is_directory=True)

    monkeypatch.setattr(import_mod, "_after_import_source_path_inspected", lambda *_: None)
    monkeypatch.setattr(import_mod, "_after_import_graph_path_inspected", replace_graph)
    with pytest.raises(
        (SnapshotImportApplyIntegrityError, SnapshotImportApplyError),
        match="changed|replaced|symlink",
    ):
        snapshot_import_apply(
            raced_real, export_g, plan_g["import_revision"], import_confirmed=True
        )

    snapshots_case = tmp_path / "snapshots-replace"
    _s, target_d, _l, _d, export_d, plan_d = _prepare(snapshots_case)
    aside_snapshots = target_d / "snapshots-aside"

    def replace_snapshots(graph_path, _graph_fd):
        if graph_path == target_d.resolve():
            (target_d / "snapshots").rename(aside_snapshots)
            shutil.copytree(aside_snapshots, target_d / "snapshots")

    monkeypatch.setattr(
        apply_mod,
        "_after_import_apply_snapshots_path_inspected",
        replace_snapshots,
    )
    with pytest.raises(
        SnapshotImportApplyIntegrityError,
        match="snapshots directory changed",
    ):
        snapshot_import_apply(
            target_d, export_d, plan_d["import_revision"], import_confirmed=True
        )
    assert _staging_leftovers(target_d) == []

    stage_case = tmp_path / "stage-replace"
    _s, target_s, _l, _d, export_s, plan_s = _prepare(stage_case)

    def replace_staging(snapshots_fd, staging_name):
        _replace_named_directory(snapshots_fd, staging_name, b"before-open")

    monkeypatch.setattr(import_mod, "_after_import_graph_path_inspected", lambda *_: None)
    monkeypatch.setattr(
        apply_mod,
        "_after_import_apply_snapshots_path_inspected",
        lambda *_: None,
    )
    monkeypatch.setattr(apply_mod, "_after_import_apply_staging_mkdir", replace_staging)
    result = snapshot_import_apply(
        target_s, export_s, plan_s["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    leftover = target_s / "snapshots" / f"{STAGING_NAME_PREFIX}{plan_s['snapshot_id']}"
    assert leftover.is_dir()
    assert (leftover / "witness.bin").read_bytes() == b"before-open"
    shutil.rmtree(leftover)


def test_pre_and_post_publication_partial_outcomes(tmp_path: Path, monkeypatch):
    import graphrag_code.snapshot_import_apply as apply_mod

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)

    def boom_write(_fd, _data):
        raise OSError(errno.EIO, "injected write failure")

    monkeypatch.setattr(apply_mod, "_write_chunk", boom_write)
    result = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["filesystem_may_have_changed"] is True
    assert result["retry_requires_fresh_plan"] is True
    assert result["staging_created"] is True
    assert result["staging_cleanup_attempted"] is True
    assert result["publication_performed"] is False
    assert isinstance(result["error"], str) and result["error"]
    for path in _staging_leftovers(target):
        shutil.rmtree(path)
    monkeypatch.setattr(apply_mod, "_write_chunk", apply_mod.os.write)

    mkdir_case = tmp_path / "mkdir-post-error"
    _s, target_m, _l, _d, export_m, plan_m = _prepare(mkdir_case)

    def fail_after_mkdir(_snapshots_fd, _staging_name):
        raise OSError(errno.EIO, "injected post-mkdir failure")

    monkeypatch.setattr(
        apply_mod, "_after_import_apply_staging_mkdir", fail_after_mkdir
    )
    result = snapshot_import_apply(
        target_m, export_m, plan_m["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["staging_created"] is True
    assert result["staging_remaining"] is True
    for path in _staging_leftovers(target_m):
        shutil.rmtree(path)
    monkeypatch.setattr(
        apply_mod, "_after_import_apply_staging_mkdir", lambda *_: None
    )

    lease_case = tmp_path / "exclusive-fail"
    _s, target_e, _l, _d, export_e, plan_e = _prepare(lease_case)
    original_exclusive = apply_mod.graph_exclusive_lease

    @contextmanager
    def fail_exclusive(_root):
        raise apply_mod.ByogPublicationLockError(
            "injected exclusive lease failure"
        )
        yield

    monkeypatch.setattr(apply_mod, "graph_exclusive_lease", fail_exclusive)
    result = snapshot_import_apply(
        target_e, export_e, plan_e["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["staging_created"] is True
    assert result["publication_performed"] is False
    for path in _staging_leftovers(target_e):
        shutil.rmtree(path)
    monkeypatch.setattr(apply_mod, "graph_exclusive_lease", original_exclusive)

    fail_cleanup = tmp_path / "cleanup-fail"
    _s, target_c, live_c, _d, export_c, plan_c = _prepare(fail_cleanup)

    def plant_foreign(_snapshots_fd, staging_name, staging_fd):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open("foreign.bin", flags, 0o600, dir_fd=staging_fd)
        os.write(fd, b"not-ours")
        os.close(fd)

    monkeypatch.setattr(apply_mod, "_after_import_apply_staging_opened", plant_foreign)
    monkeypatch.setattr(apply_mod, "_write_chunk", boom_write)
    result = snapshot_import_apply(
        target_c, export_c, plan_c["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["staging_remaining"] is True
    leftover = target_c / "snapshots" / f"{STAGING_NAME_PREFIX}{live_c.name}"
    assert leftover.is_dir()
    assert (leftover / "foreign.bin").read_bytes() == b"not-ours"
    shutil.rmtree(leftover)
    monkeypatch.setattr(apply_mod, "_write_chunk", apply_mod.os.write)
    monkeypatch.setattr(apply_mod, "_after_import_apply_staging_opened", lambda *_: None)

    verify_case = tmp_path / "post-pub"
    _s, target_v, live_v, dest_v, export_v, plan_v = _prepare(verify_case)
    current_before = _current(target_v)

    def boom_verify(*_args, **_kwargs):
        raise apply_mod.SnapshotImportApplyIntegrityError(
            "injected post-publication verification failure"
        )

    monkeypatch.setattr(apply_mod, "_after_import_apply_published", boom_verify)
    result = snapshot_import_apply(
        target_v, export_v, plan_v["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["publication_performed"] is True
    assert result["import_performed"] is True
    assert (target_v / "snapshots" / live_v.name).is_dir()
    assert _current(target_v) == current_before == dest_v.name
    assert result["staging_cleanup_attempted"] is False

    current_case = tmp_path / "post-pub-current"
    _s, target_r, live_r, _d, export_r, plan_r = _prepare(current_case)

    def rewrite_current(_snapshots_fd, _snapshot_id):
        current = target_r / "current"
        current.write_bytes(current.read_bytes())

    monkeypatch.setattr(apply_mod, "_after_import_apply_published", rewrite_current)
    result = snapshot_import_apply(
        target_r, export_r, plan_r["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["publication_performed"] is True
    assert result["snapshot_verified_after_publication"] is True
    assert result["current_before"] == result["current_after"]
    assert result["current_unchanged"] is False
    assert (target_r / "snapshots" / live_r.name).is_dir()


def test_replaced_staging_is_not_deleted(tmp_path: Path, monkeypatch):
    import graphrag_code.snapshot_import_apply as apply_mod

    _source, target, live, _dest, export_dir, plan = _prepare(tmp_path)

    def boom_write(_fd, _data):
        raise OSError(errno.EIO, "injected write failure")

    def replace_during_cleanup(snapshots_fd, staging_name):
        _replace_named_directory(snapshots_fd, staging_name, b"during-cleanup")

    monkeypatch.setattr(apply_mod, "_write_chunk", boom_write)
    monkeypatch.setattr(
        apply_mod, "_before_import_apply_staging_cleanup", replace_during_cleanup
    )
    result = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    leftover = target / "snapshots" / f"{STAGING_NAME_PREFIX}{live.name}"
    assert leftover.is_dir()
    assert (leftover / "witness.bin").read_bytes() == b"during-cleanup"
    shutil.rmtree(leftover)

    lock_case = tmp_path / "writer-lock-replace"
    _s, target_l, live_l, _d, export_l, plan_l = _prepare(lock_case)

    def replace_writer_lock(_snapshots_fd, staging_name):
        lock = (
            target_l
            / "snapshots"
            / staging_name
            / STAGING_WRITER_LOCK_NAME
        )
        lock.unlink()
        lock.write_bytes(b"replacement-lock")

    monkeypatch.setattr(
        apply_mod,
        "_before_import_apply_staging_cleanup",
        replace_writer_lock,
    )
    result = snapshot_import_apply(
        target_l, export_l, plan_l["import_revision"], import_confirmed=True
    )
    assert result["ok"] is False
    replacement_stage = (
        target_l / "snapshots" / f"{STAGING_NAME_PREFIX}{live_l.name}"
    )
    replacement_lock = replacement_stage / STAGING_WRITER_LOCK_NAME
    assert replacement_lock.read_bytes() == b"replacement-lock"
    shutil.rmtree(replacement_stage)


def test_descriptors_and_exclusive_lease_held_through_stdout(
    tmp_path: Path, monkeypatch
):
    import graphrag_code.snapshot_import_apply as apply_mod

    _source, target, _live, _dest, export_dir, plan = _prepare(tmp_path)
    original_scope = apply_mod._snapshot_import_apply_scope
    original_json = apply_mod.result_to_json
    original_format = apply_mod.format_result
    original_exclusive = apply_mod.graph_exclusive_lease
    state = {
        "exclusive": False,
        "export_fd": None,
        "graph_fd": None,
        "snapshots_fd": None,
        "payload_fds": {},
        "responses": 0,
        "flushes": 0,
        "exclusives": 0,
    }

    @contextmanager
    def tracked_exclusive(*args, **kwargs):
        state["exclusives"] += 1
        with original_exclusive(*args, **kwargs) as lease:
            state["exclusive"] = True
            try:
                yield lease
            finally:
                state["exclusive"] = False

    def capture_ready(
        _export_dir,
        _graph,
        export_fd,
        graph_fd,
        snapshots_fd,
        _staging_fd,
        payload_fds,
        exclusive_held,
        _result,
    ):
        state["export_fd"] = export_fd
        state["graph_fd"] = graph_fd
        state["snapshots_fd"] = snapshots_fd
        state["payload_fds"] = dict(payload_fds)
        os.fstat(export_fd)
        os.fstat(graph_fd)
        os.fstat(snapshots_fd)
        for fd in payload_fds.values():
            os.fstat(fd)
        assert exclusive_held is True
        assert state["exclusive"] is True

    def guarded_json(*args, **kwargs):
        assert state["exclusive"] is True
        os.fstat(state["export_fd"])
        os.fstat(state["graph_fd"])
        os.fstat(state["snapshots_fd"])
        for fd in state["payload_fds"].values():
            os.fstat(fd)
        return original_json(*args, **kwargs)

    def guarded_format(*args, **kwargs):
        assert state["exclusive"] is True
        os.fstat(state["export_fd"])
        os.fstat(state["graph_fd"])
        return original_format(*args, **kwargs)

    class GuardedStdout:
        def write(self, text: str) -> int:
            assert state["exclusive"] is True
            os.fstat(state["export_fd"])
            os.fstat(state["graph_fd"])
            state["responses"] += 1
            return len(text)

        def flush(self) -> None:
            assert state["exclusive"] is True
            os.fstat(state["export_fd"])
            os.fstat(state["graph_fd"])
            state["flushes"] += 1

    monkeypatch.setattr(apply_mod, "graph_exclusive_lease", tracked_exclusive)
    monkeypatch.setattr(apply_mod, "_after_import_apply_result_ready", capture_ready)
    monkeypatch.setattr(apply_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(apply_mod, "format_result", guarded_format)
    monkeypatch.setattr(apply_mod.sys, "stdout", GuardedStdout())
    assert (
        apply_mod.main(
            [
                "--graph",
                str(target),
                "--export-dir",
                str(export_dir),
                "--expected-import-revision",
                plan["import_revision"],
                "--import-confirmed",
                "--json",
            ]
        )
        == 0
    )
    other = tmp_path / "human"
    _s, target2, _l, _d, export2, plan2 = _prepare(other)
    assert (
        apply_mod.main(
            [
                "--graph",
                str(target2),
                "--export-dir",
                str(export2),
                "--expected-import-revision",
                plan2["import_revision"],
                "--import-confirmed",
            ]
        )
        == 0
    )
    assert state["exclusive"] is False
    assert state["exclusives"] == 2
    assert state["responses"] >= 2
    assert state["flushes"] == 2
    assert original_scope is apply_mod._snapshot_import_apply_scope


def test_no_public_producer_mcp_byog_and_contracts(tmp_path: Path, monkeypatch):
    import graphrag_code.byog_snapshot_graph_audit as audit_mod
    import graphrag_code.snapshot_compare as compare_mod
    import graphrag_code.snapshot_export_apply as export_apply
    import graphrag_code.snapshot_export_plan as export_plan
    import graphrag_code.snapshot_import_apply as apply_mod
    import graphrag_code.snapshot_import_plan as import_mod
    import graphrag_code.snapshot_read as read_mod

    _source, target, live, dest_live, export_dir, plan = _prepare(tmp_path)
    before_roots = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before_roots) == 15
    export_fp = _root_fingerprint(export_dir)
    calls = {"shared": 0, "exclusive": 0}

    def boom(*_args, **_kwargs):
        raise AssertionError("public producer or nested/read scope")

    original_shared = apply_mod.graph_read_lease
    original_exclusive = apply_mod.graph_exclusive_lease

    @contextmanager
    def counted_shared(*args, **kwargs):
        calls["shared"] += 1
        assert kwargs.get("allow_unlocked_managed") is False
        with original_shared(*args, **kwargs) as lease:
            yield lease

    @contextmanager
    def counted_exclusive(*args, **kwargs):
        calls["exclusive"] += 1
        with original_exclusive(*args, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(apply_mod, "graph_read_lease", counted_shared)
    monkeypatch.setattr(apply_mod, "graph_exclusive_lease", counted_exclusive)
    monkeypatch.setattr(import_mod, "snapshot_import_plan", boom)
    monkeypatch.setattr(export_plan, "snapshot_export_plan", boom)
    monkeypatch.setattr(export_apply, "snapshot_export_apply", boom)
    monkeypatch.setattr(read_mod, "retained_snapshot_read", boom)
    monkeypatch.setattr(compare_mod, "snapshot_history", boom, raising=False)
    monkeypatch.setattr(audit_mod, "audit_graph_root", boom)
    result = snapshot_import_apply(
        target, export_dir, plan["import_revision"], import_confirmed=True
    )
    assert result["ok"] is True
    assert calls["shared"] == 1
    assert calls["exclusive"] == 1
    assert _root_fingerprint(export_dir) == export_fp
    assert (target / "snapshots" / live.name).is_dir()
    assert _current(target) == dest_live.name
    after_roots = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert after_roots == before_roots

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
    assert "graph_exclusive_lease" in imported
    assert "staging_writer_lease" in imported
    assert "rename_directory_noreplace" in imported
    assert "_observe_fresh_import_plan" in imported
    assert HASH_CHUNK_BYTES <= 64 * 1024
    assert "dir_fd=" in source
    assert "O_EXCL" in source
    assert "O_NOFOLLOW" in source
    assert "read_bytes" not in source
    assert "rmtree" not in source
    assert "snapshot_import_plan(" not in source
    assert "os.rename(" not in source
    native = NATIVE.read_text(encoding="utf-8")
    assert "renameatx_np" in native
    assert "renameat2" in native
    assert "RENAME_EXCL" in native
    assert "os.rename(" not in native
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    from anyio import run as anyio_run

    assert len(TOOL_NAMES) == 11
    assert "snapshot_import_apply" not in TOOL_NAMES
    assert "snapshot_import_plan" not in TOOL_NAMES
    session = build_session(target, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 11
            assert "snapshot_import_apply" not in names

    anyio_run(_body)
    assert _staging_leftovers(target) == []
