"""Read-only standalone snapshot export verification.

Disposable tmp graphs and export dirs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_export_verify.py -q
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
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import publish_byog_snapshot  # type: ignore
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_export_apply import snapshot_export_apply  # type: ignore
from graphrag_code.snapshot_export_plan import (  # type: ignore
    ACCEPTED_PAYLOAD_FILES,
    HASH_CHUNK_BYTES,
    REQUIRED_PAYLOAD_FILES,
    canonical_export_revision_text,
    export_revision_of,
    snapshot_export_plan,
)
from graphrag_code.snapshot_export_verify import (  # type: ignore
    SnapshotExportVerifyError,
    SnapshotExportVerifyIntegrityError,
    format_result,
    result_to_json,
    snapshot_export_verify,
)

SCRIPT = ROOT / "scripts" / "snapshot_export_verify.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_verify.py"
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
        settings_text=f"export-verify: {marker}\n",
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


def _revision_from_dir(export_dir: Path, resolved: str) -> tuple[list[dict[str, object]], str]:
    records = []
    for name in sorted(
        (path.name for path in export_dir.iterdir() if path.is_file()),
        key=lambda item: item.encode("utf-8"),
    ):
        data = (export_dir / name).read_bytes()
        records.append(
            {
                "path": name,
                "size_bytes": len(data),
                "content_revision": "sha256:" + hashlib.sha256(data).hexdigest(),
            }
        )
    revision = export_revision_of(
        {
            "schema_version": 1,
            "resolved_snapshot": resolved,
            "files": records,
        }
    )
    return records, revision


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


def _assert_verify_shape(
    result: dict,
    export_dir: Path,
    resolved: str,
    expected: str,
    *,
    matches: bool,
) -> None:
    assert result["schema_version"] == 1
    assert result["ok"] is matches
    assert result["export_directory"] == str(export_dir.resolve())
    assert result["resolved_snapshot"] == resolved
    assert result["expected_export_revision"] == expected
    assert result["revision_matches"] is matches
    assert result["payload_verified"] is True
    assert result["export_mutated"] is False
    assert result["graph_inspected"] is False
    assert result["file_count"] == len(result["files"])
    assert result["total_size_bytes"] == sum(item["size_bytes"] for item in result["files"])
    paths = [item["path"] for item in result["files"]]
    assert paths == sorted(paths, key=lambda item: item.encode("utf-8"))
    assert set(paths) <= set(ACCEPTED_PAYLOAD_FILES)
    assert set(REQUIRED_PAYLOAD_FILES) <= set(paths)
    for item in result["files"]:
        data = (export_dir / item["path"]).read_bytes()
        assert item["size_bytes"] == len(data)
        assert item["content_revision"] == "sha256:" + hashlib.sha256(data).hexdigest()
    assert result["observed_export_revision"] == export_revision_of(
        {
            "schema_version": 1,
            "resolved_snapshot": resolved,
            "files": result["files"],
        }
    )
    if matches:
        assert result["observed_export_revision"] == expected
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == [
        "verify_is_not_backup",
        "export_revision_is_self_consistency_only",
        "export_not_mutated",
        "observation_window_only",
        "cli_only_not_mcp",
    ]
    text = format_result(result)
    assert str(export_dir.resolve()) in text
    assert resolved in text
    assert expected in text
    assert result["observed_export_revision"] in text
    assert f"files={result['file_count']}" in text
    assert f"total_size_bytes={result['total_size_bytes']}" in text
    assert f"revision_matches={str(matches).lower()}" in text
    assert "not a backup" in text
    assert "not authorization to delete" in text


def test_required_only_and_optional_payload_exports(tmp_path: Path):
    graph = tmp_path / "g"
    required_snap = _publish(graph, "required")
    required_dir = _copy_standalone(
        required_snap, tmp_path / "required-only", names=set(REQUIRED_PAYLOAD_FILES)
    )
    resolved = json.loads((required_dir / "manifest.json").read_text(encoding="utf-8"))["id"]
    _records, required_revision = _revision_from_dir(required_dir, resolved)
    required = snapshot_export_verify(required_dir, required_revision)
    _assert_verify_shape(required, required_dir, resolved, required_revision, matches=True)
    assert {item["path"] for item in required["files"]} == set(REQUIRED_PAYLOAD_FILES)
    assert "settings.yaml" not in {item["path"] for item in required["files"]}
    assert "call_observations.parquet" not in {item["path"] for item in required["files"]}

    optional_graph = tmp_path / "optional-g"
    optional_snap = _publish(optional_graph, "optional", observations=True)
    optional_dir = _copy_standalone(optional_snap, tmp_path / "optional")
    optional_plan = snapshot_export_plan(optional_graph, optional_snap.name)
    optional = snapshot_export_verify(optional_dir, optional_plan["export_revision"])
    _assert_verify_shape(
        optional,
        optional_dir,
        optional_snap.name,
        optional_plan["export_revision"],
        matches=True,
    )
    assert {item["path"] for item in optional["files"]} == {
        "call_observations.parquet",
        "entities.parquet",
        "manifest.json",
        "relationships.parquet",
        "settings.yaml",
        "text_units.parquet",
    }


def test_apply_output_and_plan_revision_compatibility(tmp_path: Path):
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
    verified = snapshot_export_verify(dest, plan["export_revision"])
    _assert_verify_shape(verified, dest, live.name, plan["export_revision"], matches=True)
    assert verified["observed_export_revision"] == plan["export_revision"]
    assert verified["observed_export_revision"] == applied["observed_export_revision"]
    assert verified["files"] == plan["files"]
    assert verified["files"] == applied["files"]
    assert export_revision_of(plan) == plan["export_revision"]
    assert json.loads(canonical_export_revision_text(plan))["files"] == verified["files"]


def test_expected_revision_mismatch_emits_exit_1_report(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "mismatch")
    export_dir = _copy_standalone(snap, tmp_path / "export")
    plan = snapshot_export_plan(graph, snap.name)
    other = "sha256:" + ("ab" * 32)
    assert other != plan["export_revision"]
    result = snapshot_export_verify(export_dir, other)
    _assert_verify_shape(result, export_dir, snap.name, other, matches=False)
    assert result["observed_export_revision"] == plan["export_revision"]
    proc = _run(
        "--export-dir",
        str(export_dir),
        "--expected-export-revision",
        other,
        "--json",
    )
    assert proc.returncode == 1
    body = json.loads(proc.stdout)
    assert body["ok"] is False
    assert body["revision_matches"] is False
    assert body["payload_verified"] is True
    assert body["observed_export_revision"] == plan["export_revision"]
    assert result_to_json(body) == proc.stdout


def test_malformed_expected_revisions_and_missing_args(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "only")
    export_dir = _copy_standalone(snap, tmp_path / "export")
    missing = _run()
    assert missing.returncode == 2
    assert missing.stdout == ""
    for token in (
        "",
        "sha256:" + ("AB" * 32),
        " sha256:" + ("ab" * 32),
        "sha256:" + ("ab" * 32) + " ",
        "sha256:" + ("ab" * 31),
        "md5:" + ("ab" * 16),
        "sha256:" + ("ab" * 32) + "\n",
    ):
        proc = _run(
            "--export-dir",
            str(export_dir),
            "--expected-export-revision",
            token,
            "--json",
        )
        assert proc.returncode == 2
        assert proc.stdout == ""
    with pytest.raises(SnapshotExportVerifyError, match="sha256"):
        snapshot_export_verify(export_dir, "sha256:" + ("AB" * 32))


def test_relative_cwd_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    graph = here / "g"
    snap = _publish(graph, "only")
    export_dir = _copy_standalone(snap, here / "export")
    plan = snapshot_export_plan(graph, snap.name)
    args = [
        "--export-dir",
        "export",
        "--expected-export-revision",
        plan["export_revision"],
        "--json",
    ]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_export_verify", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-export-verify", *args],
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
    assert bodies[0]["export_directory"] == str(export_dir.resolve())
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-export-verify",
            "--export-dir",
            str(export_dir),
            "--expected-export-revision",
            plan["export_revision"],
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
    assert "snapshot_export_verify.py" in names


def test_export_directory_symlink_rejected(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "only")
    export_dir = _copy_standalone(snap, tmp_path / "export")
    plan = snapshot_export_plan(graph, snap.name)
    linked = tmp_path / "linked"
    linked.symlink_to(export_dir, target_is_directory=True)
    with pytest.raises(SnapshotExportVerifyError, match="symlink"):
        snapshot_export_verify(linked, plan["export_revision"])
    proc = _run(
        "--export-dir",
        str(linked),
        "--expected-export-revision",
        plan["export_revision"],
        "--json",
    )
    assert proc.returncode == 2
    assert proc.stdout == ""


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
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="symlink"):
        snapshot_export_verify(export_dir, plan["export_revision"])
    proc = _run(
        "--export-dir",
        str(export_dir),
        "--expected-export-revision",
        plan["export_revision"],
        "--json",
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert outside.read_bytes() == b"must-not-follow"


def test_directory_replacement_never_reads_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_plan as plan_mod
    import graphrag_code.snapshot_export_verify as verify_mod

    graph = tmp_path / "g"
    snap = _publish(graph, "anchored")
    plan = snapshot_export_plan(graph, snap.name)

    # Replacement between initial lstat and canonicalization must not redirect
    # the descriptor open to a different, otherwise valid export directory.
    preanchor = _copy_standalone(snap, tmp_path / "preanchor")
    pre_replacement = _copy_standalone(snap, tmp_path / "pre-replacement")
    pre_replacement_inodes = {
        path.stat().st_ino for path in pre_replacement.iterdir() if path.is_file()
    }
    pre_hidden = tmp_path / "pre-hidden-original"

    def replace_before_anchor(path):
        if path == preanchor:
            preanchor.rename(pre_hidden)
            preanchor.symlink_to(pre_replacement, target_is_directory=True)

    original_read = plan_mod.os.read

    def reject_pre_replacement(fd, count):
        assert os.fstat(fd).st_ino not in pre_replacement_inodes
        return original_read(fd, count)

    monkeypatch.setattr(verify_mod, "_after_export_verify_path_inspected", replace_before_anchor)
    monkeypatch.setattr(plan_mod.os, "read", reject_pre_replacement)
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="changed|symlink|unsafe"):
        snapshot_export_verify(preanchor, plan["export_revision"])

    monkeypatch.setattr(verify_mod, "_after_export_verify_path_inspected", lambda *_: None)
    export_dir = _copy_standalone(snap, tmp_path / "export")
    replacement = tmp_path / "replacement"
    _copy_standalone(snap, replacement)
    (replacement / "entities.parquet").write_bytes(b"replacement-bytes")
    replacement_inodes = {
        path.stat().st_ino for path in replacement.iterdir() if path.is_file()
    }
    hidden = tmp_path / "hidden-original"

    def replace_export(_export_dir, _records):
        export_dir.rename(hidden)
        export_dir.symlink_to(replacement, target_is_directory=True)

    def reject_replacement(fd, count):
        assert os.fstat(fd).st_ino not in replacement_inodes
        return original_read(fd, count)

    monkeypatch.setattr(verify_mod, "_after_export_verify_first_observation", replace_export)
    monkeypatch.setattr(plan_mod.os, "read", reject_replacement)
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="replaced|changed"):
        snapshot_export_verify(export_dir, plan["export_revision"])
    assert (replacement / "entities.parquet").read_bytes() == b"replacement-bytes"


def test_missing_unexpected_nested_fifo_socket_and_directory_entries(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "layout")
    plan = snapshot_export_plan(graph, snap.name)

    missing = _copy_standalone(snap, tmp_path / "missing")
    (missing / "entities.parquet").unlink()
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="missing"):
        snapshot_export_verify(missing, plan["export_revision"])
    missing_proc = _run(
        "--export-dir",
        str(missing),
        "--expected-export-revision",
        plan["export_revision"],
        "--json",
    )
    assert missing_proc.returncode == 1
    assert missing_proc.stdout == ""

    unexpected = _copy_standalone(snap, tmp_path / "unexpected")
    (unexpected / "notes.txt").write_text("nope\n", encoding="utf-8")
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="unexpected"):
        snapshot_export_verify(unexpected, plan["export_revision"])

    nested = _copy_standalone(snap, tmp_path / "nested")
    (nested / "nested-dir").mkdir()
    (nested / "nested-dir" / "entities.parquet").write_bytes(b"hidden")
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="unexpected|not a regular"):
        snapshot_export_verify(nested, plan["export_revision"])

    fifo_dir = _copy_standalone(snap, tmp_path / "fifo")
    os.mkfifo(fifo_dir / "pipe.fifo")
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="unexpected|not a regular"):
        snapshot_export_verify(fifo_dir, plan["export_revision"])

    short = Path(f"/tmp/sev{os.getpid()}")
    if short.exists():
        shutil.rmtree(short)
    short.mkdir()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        socket_dir = _copy_standalone(snap, short / "e")
        server.bind(str(socket_dir / "s"))
        with pytest.raises(SnapshotExportVerifyIntegrityError, match="unexpected|not a regular"):
            snapshot_export_verify(socket_dir, plan["export_revision"])
    finally:
        server.close()
        shutil.rmtree(short, ignore_errors=True)

    directory_payload = _copy_standalone(snap, tmp_path / "dir-payload")
    (directory_payload / "settings.yaml").unlink()
    (directory_payload / "settings.yaml").mkdir()
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="not a regular"):
        snapshot_export_verify(directory_payload, plan["export_revision"])


def test_manifest_id_schema_and_files_disagreement(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "manifest")
    plan = snapshot_export_plan(graph, snap.name)

    bad_id = _copy_standalone(snap, tmp_path / "bad-id")
    payload = json.loads((bad_id / "manifest.json").read_text(encoding="utf-8"))
    payload["id"] = "."
    (bad_id / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="manifest id|canonical"):
        snapshot_export_verify(bad_id, plan["export_revision"])

    bad_schema = _copy_standalone(snap, tmp_path / "bad-schema")
    payload = json.loads((bad_schema / "manifest.json").read_text(encoding="utf-8"))
    payload["schema_version"] = 2
    (bad_schema / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="schema_version"):
        snapshot_export_verify(bad_schema, plan["export_revision"])

    bad_files = _copy_standalone(snap, tmp_path / "bad-files")
    payload = json.loads((bad_files / "manifest.json").read_text(encoding="utf-8"))
    payload["files"] = ["entities.parquet"]
    (bad_files / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="manifest files"):
        snapshot_export_verify(bad_files, plan["export_revision"])


def test_same_size_rewrite_and_between_observation_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_verify as verify_mod

    graph = tmp_path / "g"
    snap = _publish(graph, "stable")
    export_dir = _copy_standalone(snap, tmp_path / "same-size")
    plan = snapshot_export_plan(graph, snap.name)
    target = export_dir / "entities.parquet"
    original = target.read_bytes()
    original_stat = target.stat()

    def rewrite_same_size(_export_dir, _records):
        replacement = bytes([original[0] ^ 1]) + original[1:]
        target.write_bytes(replacement)
        os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    monkeypatch.setattr(verify_mod, "_after_export_verify_first_observation", rewrite_same_size)
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="changed"):
        snapshot_export_verify(export_dir, plan["export_revision"])

    listing_dir = _copy_standalone(snap, tmp_path / "listing")
    listing_plan = snapshot_export_plan(graph, snap.name)

    def add_unexpected(_export_dir, _records):
        (listing_dir / "notes.txt").write_text("extra\n", encoding="utf-8")

    monkeypatch.setattr(verify_mod, "_after_export_verify_first_observation", add_unexpected)
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="unexpected|changed"):
        snapshot_export_verify(listing_dir, listing_plan["export_revision"])

    manifest_dir = _copy_standalone(snap, tmp_path / "manifest-change")
    manifest_plan = snapshot_export_plan(graph, snap.name)

    def rewrite_manifest(_export_dir, _records):
        payload = json.loads((manifest_dir / "manifest.json").read_text(encoding="utf-8"))
        payload["created"] = "changed"
        (manifest_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(verify_mod, "_after_export_verify_first_observation", rewrite_manifest)
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="manifest|changed"):
        snapshot_export_verify(manifest_dir, manifest_plan["export_revision"])


def test_short_reads_still_hash_complete_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_plan as plan_mod

    graph = tmp_path / "g"
    snap = _publish(graph, "short")
    export_dir = _copy_standalone(snap, tmp_path / "short")
    plan = snapshot_export_plan(graph, snap.name)
    original_read = plan_mod.os.read

    def one_byte_reads(fd, size):
        return original_read(fd, 1 if size > 1 else size)

    monkeypatch.setattr(plan_mod.os, "read", one_byte_reads)
    result = snapshot_export_verify(export_dir, plan["export_revision"])
    assert result["ok"] is True
    assert result["observed_export_revision"] == plan["export_revision"]

    def truncated_read(_fd, _size):
        return b""

    monkeypatch.setattr(plan_mod.os, "read", truncated_read)
    with pytest.raises(SnapshotExportVerifyIntegrityError, match="changed|did not match"):
        snapshot_export_verify(export_dir, plan["export_revision"])


def test_deterministic_byte_order_and_json(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "order")
    export_dir = _copy_standalone(snap, tmp_path / "order")
    plan = snapshot_export_plan(graph, snap.name)
    first = snapshot_export_verify(export_dir, plan["export_revision"])
    second = snapshot_export_verify(export_dir, plan["export_revision"])
    assert first == second
    assert result_to_json(first) == result_to_json(second)
    names = [item["path"] for item in first["files"]]
    assert names == sorted(names, key=lambda item: item.encode("utf-8"))
    digest = hashlib.sha256(
        canonical_export_revision_text(
            {
                "schema_version": 1,
                "resolved_snapshot": first["resolved_snapshot"],
                "files": first["files"],
            }
        ).encode("utf-8")
    ).hexdigest()
    assert first["observed_export_revision"] == "sha256:" + digest


def test_descriptor_lifetime_through_serialization_write_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_verify as verify_mod

    graph = tmp_path / "g"
    snap = _publish(graph, "held")
    export_dir = _copy_standalone(snap, tmp_path / "held")
    plan = snapshot_export_plan(graph, snap.name)
    original_json = verify_mod.result_to_json
    original_format = verify_mod.format_result
    state = {"fd": None, "responses": 0, "flushes": 0}

    def capture_ready(_export_dir, directory_fd, _result):
        state["fd"] = directory_fd
        os.fstat(directory_fd)

    def guarded_json(*args, **kwargs):
        assert state["fd"] is not None
        os.fstat(state["fd"])
        return original_json(*args, **kwargs)

    def guarded_format(*args, **kwargs):
        assert state["fd"] is not None
        os.fstat(state["fd"])
        return original_format(*args, **kwargs)

    class GuardedStdout:
        def write(self, text: str) -> int:
            os.fstat(state["fd"])
            state["responses"] += 1
            return len(text)

        def flush(self) -> None:
            os.fstat(state["fd"])
            state["flushes"] += 1

    monkeypatch.setattr(verify_mod, "_after_export_verify_result_ready", capture_ready)
    monkeypatch.setattr(verify_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(verify_mod, "format_result", guarded_format)
    monkeypatch.setattr(verify_mod.sys, "stdout", GuardedStdout())
    assert (
        verify_mod.main(
            [
                "--export-dir",
                str(export_dir),
                "--expected-export-revision",
                plan["export_revision"],
                "--json",
            ]
        )
        == 0
    )
    assert (
        verify_mod.main(
            [
                "--export-dir",
                str(export_dir),
                "--expected-export-revision",
                plan["export_revision"],
            ]
        )
        == 0
    )
    assert state["responses"] >= 2
    assert state["flushes"] == 2


def test_implementation_streams_and_does_not_mutate_or_invoke_producers():
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
    assert "_open_directory_nofollow" in imported
    assert "_payload_children" in imported
    assert "_stream_regular_file" in imported
    assert "_require_descriptor_reads" in imported
    assert "graph_read_lease" not in source
    assert "graph_exclusive_lease" not in source
    assert "snapshot_export_plan(" not in source
    assert "snapshot_export_apply(" not in source
    assert "publish_byog_snapshot" not in source
    assert "read_bytes" not in source
    assert HASH_CHUNK_BYTES <= 64 * 1024
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    human = format_result(
        {
            "export_directory": "/tmp/x",
            "resolved_snapshot": "id",
            "expected_export_revision": "sha256:" + ("00" * 32),
            "observed_export_revision": "sha256:" + ("00" * 32),
            "file_count": 0,
            "total_size_bytes": 0,
            "revision_matches": True,
            "payload_verified": True,
            "ok": True,
        }
    )
    assert "not a backup" in human
    assert "recoverable" not in human.lower()
    assert "authentic" not in human.lower()


def test_mcp_remains_exactly_seventeen_and_byog_roots_unchanged(tmp_path: Path):
    from anyio import run as anyio_run

    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    assert len(TOOL_NAMES) == 17
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
            assert len(names) == 17
            assert "snapshot_export_verify" not in names

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
