"""CAS-guarded snapshot export apply.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_export_apply.py -q
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
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import (  # type: ignore
    PUBLICATION_LOCK_NAME,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_export_apply import (  # type: ignore
    HASH_CHUNK_BYTES,
    SnapshotExportApplyError,
    SnapshotExportApplyIntegrityError,
    format_result,
    result_to_json,
    snapshot_export_apply,
)
from graphrag_code.snapshot_export_plan import (  # type: ignore
    SnapshotExportPlanIntegrityError,
    snapshot_export_plan,
)
from graphrag_code.snapshot_pins import OPERATOR_PINS_NAME  # type: ignore

SCRIPT = ROOT / "scripts" / "snapshot_export_apply.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_apply.py"
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
        "graph_exclusive_lease",
        "_publication_lock",
        "staging_writer_lease",
        "rmtree",
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


def _publish(graph: Path, marker: str, *, keep_last: int = 10, observations: bool = False) -> Path:
    ents, rels, tus, obs = _rows(marker)
    return publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text=f"export-apply: {marker}\n",
        keep_last=keep_last,
        call_observations_df=pd.DataFrame(obs) if observations else None,
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


def _leftover_staging(parent: Path) -> list[Path]:
    if not parent.exists() or not parent.is_dir():
        return []
    return sorted(
        path
        for path in parent.iterdir()
        if path.name.startswith(".graphrag-export-")
    )


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


def _file_revision(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), "sha256:" + hashlib.sha256(data).hexdigest()


def _assert_apply_shape(
    result: dict,
    graph: Path,
    requested: str,
    resolved: str,
    destination: Path,
    expected: str,
) -> None:
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["graph"] == str(graph)
    assert result["requested_snapshot"] == requested
    assert result["resolved_snapshot"] == resolved
    assert result["destination"] == str(destination)
    assert result["export_confirmed"] is True
    assert result["export_performed"] is True
    assert result["destination_created"] is True
    assert result["destination_verified"] is True
    assert result["source_unchanged"] is True
    assert result["partial"] is False
    assert result["parent_fsync_confirmed"] is True
    assert result["error"] is None
    assert result["expected_export_revision"] == expected
    assert result["observed_export_revision"] == expected
    assert result["file_count"] == len(result["files"])
    assert result["total_size_bytes"] == sum(item["size_bytes"] for item in result["files"])
    paths = [item["path"] for item in result["files"]]
    assert paths == sorted(paths, key=lambda item: item.encode("utf-8"))
    snap = graph / "snapshots" / resolved
    expected_total = 0
    for item in result["files"]:
        src_size, src_rev = _file_revision(snap / item["path"])
        dst_size, dst_rev = _file_revision(destination / item["path"])
        assert item["size_bytes"] == src_size == dst_size
        assert item["content_revision"] == src_rev == dst_rev
        expected_total += src_size
    assert result["total_size_bytes"] == expected_total
    dest_names = sorted(
        path.name for path in destination.iterdir() if not path.name.startswith(".")
    )
    assert dest_names == paths
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == [
        "export_is_not_backup",
        "export_revision_is_cas_only",
        "metadata_not_preserved",
        "crash_may_leave_private_staging",
        "export_writer_lease_not_ownership",
        "advisory_locks_cooperating_only",
        "cli_only_not_mcp",
    ]
    text = format_result(result)
    assert "export_performed=true" in text
    assert "destination_verified=true" in text
    assert "not a backup" in text
    assert "not authorization to delete" in text
    assert "recover" not in text.lower()
    assert "authentic" not in text.lower()


def test_byte_exact_required_and_optional_payloads(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "old", observations=True)
    live = _publish(graph, "new", observations=True)
    before = _protected_state(graph)
    current_plan = snapshot_export_plan(graph, "current")
    dest_current = tmp_path / "exports" / "current-copy"
    dest_current.parent.mkdir()
    current = snapshot_export_apply(
        graph,
        "current",
        dest_current,
        current_plan["export_revision"],
        export_confirmed=True,
    )
    _assert_apply_shape(
        current, graph, "current", live.name, dest_current, current_plan["export_revision"]
    )
    assert {item["path"] for item in current["files"]} == {
        "call_observations.parquet",
        "entities.parquet",
        "manifest.json",
        "relationships.parquet",
        "settings.yaml",
        "text_units.parquet",
    }
    retained_plan = snapshot_export_plan(graph, first.name)
    dest_retained = tmp_path / "exports" / "retained-copy"
    retained = snapshot_export_apply(
        graph,
        first.name,
        dest_retained,
        retained_plan["export_revision"],
        export_confirmed=True,
    )
    _assert_apply_shape(
        retained, graph, first.name, first.name, dest_retained, retained_plan["export_revision"]
    )
    assert retained["observed_export_revision"] != current["observed_export_revision"]
    assert _protected_state(graph) == before
    assert first.is_dir() and live.is_dir()


def test_revision_mismatch_and_malformed_expected(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "only")
    dest = tmp_path / "out"
    before = _protected_state(graph)
    other = "sha256:" + ("ab" * 32)
    mismatch = _run(
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--destination",
        str(dest),
        "--expected-export-revision",
        other,
        "--export-confirmed",
        "--json",
    )
    assert mismatch.returncode == 2
    assert mismatch.stdout == ""
    assert not dest.exists()
    for token in (
        "",
        "sha256:" + ("AB" * 32),
        " sha256:" + ("ab" * 32),
        "sha256:" + ("ab" * 32) + " ",
        "sha256:" + ("ab" * 31),
        "md5:" + ("ab" * 16),
    ):
        proc = _run(
            "--graph",
            str(graph),
            "--snapshot",
            "current",
            "--destination",
            str(dest),
            "--expected-export-revision",
            token,
            "--export-confirmed",
            "--json",
        )
        assert proc.returncode == 2
        assert proc.stdout == ""
        assert not dest.exists()
    assert _protected_state(graph) == before


def test_missing_confirmation_changes_nothing(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "out"
    before = _protected_state(graph)
    missing = _run(
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--destination",
        str(dest),
        "--expected-export-revision",
        plan["export_revision"],
        "--json",
    )
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "--export-confirmed" in missing.stderr
    assert not dest.exists()
    assert _protected_state(graph) == before


def test_preexisting_destination_file_dir_symlink_and_fifo(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    before = _protected_state(graph)
    parent = tmp_path / "dests"
    parent.mkdir()

    as_file = parent / "as-file"
    as_file.write_bytes(b"keep-me")
    as_dir = parent / "as-dir"
    as_dir.mkdir()
    (as_dir / "inside").write_bytes(b"nested")
    as_link = parent / "as-link"
    as_link.symlink_to(as_file)
    as_fifo = parent / "as-fifo"
    os.mkfifo(as_fifo)

    for dest in (as_file, as_dir, as_link, as_fifo):
        proc = _run(
            "--graph",
            str(graph),
            "--snapshot",
            "current",
            "--destination",
            str(dest),
            "--expected-export-revision",
            plan["export_revision"],
            "--export-confirmed",
            "--json",
        )
        assert proc.returncode == 2
        assert proc.stdout == ""
    assert as_file.read_bytes() == b"keep-me"
    assert (as_dir / "inside").read_bytes() == b"nested"
    assert as_link.is_symlink()
    assert stat.S_ISFIFO(as_fifo.lstat().st_mode)
    assert _protected_state(graph) == before


def test_destination_introduced_before_publication_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "race-dest"
    before = _protected_state(graph)

    def plant_destination(_parent_fd, dest_name, _staging_name):
        planted = dest.parent / dest_name
        planted.mkdir()
        (planted / "witness").write_bytes(b"concurrent")

    monkeypatch.setattr(apply_mod, "_before_export_apply_publication", plant_destination)
    with pytest.raises(SnapshotExportApplyError, match="already exists"):
        snapshot_export_apply(
            graph,
            "current",
            dest,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert dest.is_dir()
    assert (dest / "witness").read_bytes() == b"concurrent"
    assert not (dest / "entities.parquet").exists()
    leftovers = [
        path
        for path in dest.parent.iterdir()
        if path.name.startswith(apply_mod.STAGING_NAME_PREFIX)
    ]
    assert leftovers == []
    assert _protected_state(graph) == before


def test_source_symlink_and_selected_directory_replacement_never_reads_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod
    import graphrag_code.snapshot_export_plan as plan_mod

    graph = tmp_path / "g"
    live = _publish(graph, "only")
    dest = tmp_path / "out-replaced"
    outside = tmp_path / "outside-snapshot"
    shutil.copytree(live, outside)
    outside_inodes = {path.stat().st_ino for path in outside.iterdir() if path.is_file()}
    hidden = graph / "snapshots" / "hidden-original"
    plan = snapshot_export_plan(graph, "current")

    def replace_selected_directory(_root, _plan):
        live.rename(hidden)
        live.symlink_to(outside, target_is_directory=True)

    original_read = apply_mod.os.read

    def reject_outside_descriptor(fd, count):
        assert os.fstat(fd).st_ino not in outside_inodes
        return original_read(fd, count)

    monkeypatch.setattr(apply_mod, "_after_export_apply_plan_computed", replace_selected_directory)
    monkeypatch.setattr(apply_mod.os, "read", reject_outside_descriptor)
    monkeypatch.setattr(plan_mod.os, "read", reject_outside_descriptor)
    with pytest.raises(
        (SnapshotExportApplyIntegrityError, SnapshotExportApplyError),
        match="snapshot|symlink|changed|listing",
    ):
        snapshot_export_apply(
            graph,
            live.name,
            dest,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dest.exists()
    assert outside_inodes == {path.stat().st_ino for path in outside.iterdir() if path.is_file()}

    graph2 = tmp_path / "g2"
    live2 = _publish(graph2, "link")
    dest2 = tmp_path / "out2"
    dummy_revision = "sha256:" + ("ab" * 32)
    outside_file = tmp_path / "outside.parquet"
    outside_file.write_bytes(b"must-not-follow")
    payload = live2 / "entities.parquet"
    payload.unlink()
    payload.symlink_to(outside_file)
    with pytest.raises(SnapshotExportApplyError, match="symlink"):
        snapshot_export_apply(
            graph2,
            "current",
            dest2,
            dummy_revision,
            export_confirmed=True,
        )
    assert not dest2.exists()
    assert outside_file.read_bytes() == b"must-not-follow"


def test_same_size_mutation_and_source_listing_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    live = _publish(graph, "same-size")
    dest = tmp_path / "out"
    plan = snapshot_export_plan(graph, "current")
    target = live / "entities.parquet"
    original = target.read_bytes()
    original_stat = target.stat()

    def rewrite_same_size(_root, _records):
        replacement = bytes([original[0] ^ 1]) + original[1:]
        target.write_bytes(replacement)
        os.utime(target, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))

    monkeypatch.setattr(apply_mod, "_after_export_apply_copied", rewrite_same_size)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="content changed|did not match"):
        snapshot_export_apply(
            graph,
            "current",
            dest,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dest.exists()

    listing_graph = tmp_path / "listing"
    listing_live = _publish(listing_graph, "listing")
    listing_dest = tmp_path / "listing-out"
    listing_plan = snapshot_export_plan(listing_graph, "current")

    def add_published(_root, _records):
        extra = listing_graph / "snapshots" / "19990101-000000-addedone"
        if not extra.exists():
            shutil.copytree(listing_live, extra)

    monkeypatch.setattr(apply_mod, "_after_export_apply_copied", add_published)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="listing|lock|current|changed"):
        snapshot_export_apply(
            listing_graph,
            listing_live.name,
            listing_dest,
            listing_plan["export_revision"],
            export_confirmed=True,
        )
    assert not listing_dest.exists()

    manifest_graph = tmp_path / "manifest"
    manifest_live = _publish(manifest_graph, "manifest")
    manifest_dest = tmp_path / "manifest-out"
    manifest_plan = snapshot_export_plan(manifest_graph, "current")

    def rewrite_manifest(_root, _records):
        path = manifest_live / "manifest.json"
        parsed = json.loads(path.read_text(encoding="utf-8"))
        parsed["counts"]["entities"] = int(parsed["counts"]["entities"]) + 1
        path.write_text(json.dumps(parsed), encoding="utf-8")

    monkeypatch.setattr(apply_mod, "_after_export_apply_copied", rewrite_manifest)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="changed|did not match"):
        snapshot_export_apply(
            manifest_graph,
            "current",
            manifest_dest,
            manifest_plan["export_revision"],
            export_confirmed=True,
        )
    assert not manifest_dest.exists()

    current_graph = tmp_path / "current-race"
    first = _publish(current_graph, "old")
    live = _publish(current_graph, "new")
    current_dest = tmp_path / "current-out"
    current_plan = snapshot_export_plan(current_graph, "current")

    def retarget_current(_root, _records):
        (current_graph / "current").write_text(first.name + "\n", encoding="utf-8")

    monkeypatch.setattr(apply_mod, "_after_export_apply_copied", retarget_current)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="current|listing|lock"):
        snapshot_export_apply(
            current_graph,
            "current",
            current_dest,
            current_plan["export_revision"],
            export_confirmed=True,
        )
    assert not current_dest.exists()
    assert live.is_dir()


def test_short_reads_and_injected_io_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "short")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "short-out"
    original_read = apply_mod._read_chunk

    def one_byte_reads(fd, size):
        return original_read(fd, 1 if size > 1 else size)

    monkeypatch.setattr(apply_mod, "_read_chunk", one_byte_reads)
    result = snapshot_export_apply(
        graph,
        "current",
        dest,
        plan["export_revision"],
        export_confirmed=True,
    )
    assert result["ok"] is True
    assert dest.is_dir()

    fail_graph = tmp_path / "fail"
    _publish(fail_graph, "fail")
    fail_plan = snapshot_export_plan(fail_graph, "current")
    fail_dest = tmp_path / "fail-out"

    def truncated_read(_fd, _size):
        return b""

    monkeypatch.setattr(apply_mod, "_read_chunk", truncated_read)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="did not match"):
        snapshot_export_apply(
            fail_graph,
            "current",
            fail_dest,
            fail_plan["export_revision"],
            export_confirmed=True,
        )
    assert not fail_dest.exists()

    monkeypatch.setattr(apply_mod, "_read_chunk", original_read)

    def boom_write(_fd, _data):
        raise OSError(errno.EIO, "injected write failure")

    monkeypatch.setattr(apply_mod, "_write_chunk", boom_write)
    write_dest = tmp_path / "write-out"
    with pytest.raises(SnapshotExportApplyError, match="write|EIO|injected"):
        snapshot_export_apply(
            fail_graph,
            "current",
            write_dest,
            fail_plan["export_revision"],
            export_confirmed=True,
        )
    assert not write_dest.exists()
    assert _leftover_staging(write_dest.parent) == []
    monkeypatch.setattr(apply_mod, "_write_chunk", apply_mod.os.write)

    def boom_fsync(_fd):
        raise OSError(errno.EIO, "injected fsync failure")

    monkeypatch.setattr(apply_mod, "_fsync", boom_fsync)
    fsync_dest = tmp_path / "fsync-out"
    with pytest.raises(SnapshotExportApplyError, match="fsync|EIO|injected"):
        snapshot_export_apply(
            fail_graph,
            "current",
            fsync_dest,
            fail_plan["export_revision"],
            export_confirmed=True,
        )
    assert not fsync_dest.exists()
    assert _leftover_staging(fsync_dest.parent) == []
    monkeypatch.setattr(apply_mod, "_fsync", apply_mod.os.fsync)

    def boom_publish(*_args, **_kwargs):
        raise apply_mod.RenameNoreplaceError(errno.EIO, "injected publication failure")

    monkeypatch.setattr(apply_mod, "_publish_staging", boom_publish)
    pub_dest = tmp_path / "pub-out"
    with pytest.raises(SnapshotExportApplyError, match="publication failed"):
        snapshot_export_apply(
            fail_graph,
            "current",
            pub_dest,
            fail_plan["export_revision"],
            export_confirmed=True,
        )
    assert not pub_dest.exists()
    assert _leftover_staging(pub_dest.parent) == []


def test_staged_copy_verification_failure_and_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "stage")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "stage-out"
    parent = dest.parent
    decoy = parent / (apply_mod.STAGING_NAME_PREFIX + "decoy")
    decoy.mkdir()
    decoy_file = decoy / "keep.txt"
    decoy_file.write_bytes(b"not-ours")
    extra_holder: dict[str, Path] = {}

    def plant_extra(_parent_fd, staging_name, staging_fd):
        extra_name = "unexpected.bin"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        extra_fd = os.open(extra_name, flags, 0o600, dir_fd=staging_fd)
        os.write(extra_fd, b"foreign")
        os.close(extra_fd)
        extra_holder["staging"] = parent / staging_name
        extra_holder["extra"] = parent / staging_name / extra_name

    def corrupt_staged(_root, _records):
        staging = extra_holder["staging"]
        payload = staging / "entities.parquet"
        payload.write_bytes(payload.read_bytes() + b"X")

    monkeypatch.setattr(apply_mod, "_after_export_apply_staging_created", plant_extra)
    monkeypatch.setattr(apply_mod, "_after_export_apply_copied", corrupt_staged)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="staged payload|listing"):
        snapshot_export_apply(
            graph,
            "current",
            dest,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dest.exists()
    assert decoy_file.read_bytes() == b"not-ours"
    assert decoy.is_dir()
    leftover = extra_holder.get("extra")
    assert leftover is not None and leftover.is_file()
    assert leftover.read_bytes() == b"foreign"


def test_exactly_one_shared_lease_no_nested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_compare as compare_mod
    import graphrag_code.snapshot_export_apply as apply_mod
    import graphrag_code.snapshot_export_plan as plan_mod
    import graphrag_code.snapshot_read as read_mod
    import graphrag_code.byog_snapshot_graph_audit as audit_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "lease-out"
    calls = {"shared": 0}
    state = {"lease_active": False}
    original = apply_mod.graph_read_lease

    @contextmanager
    def counted(*args, **kwargs):
        calls["shared"] += 1
        with original(*args, **kwargs) as lease:
            state["lease_active"] = True
            try:
                yield lease
            finally:
                state["lease_active"] = False

    def boom(*_args, **_kwargs):
        raise AssertionError("nested graph lease or public mutating/read scope")

    monkeypatch.setattr(apply_mod, "graph_read_lease", counted)
    monkeypatch.setattr(apply_mod, "graph_exclusive_lease", boom, raising=False)
    monkeypatch.setattr(plan_mod, "snapshot_export_plan", boom)
    monkeypatch.setattr(plan_mod, "graph_read_lease", boom)
    monkeypatch.setattr(read_mod, "retained_snapshot_read", boom)
    monkeypatch.setattr(compare_mod, "snapshot_history", boom, raising=False)
    monkeypatch.setattr(compare_mod, "graph_read_lease", boom)
    monkeypatch.setattr(audit_mod, "audit_graph_root", boom)
    monkeypatch.setattr(audit_mod, "resolve_snapshot", boom)
    result = snapshot_export_apply(
        graph,
        "current",
        dest,
        plan["export_revision"],
        export_confirmed=True,
    )
    assert result["ok"] is True
    assert dest.is_dir()
    assert calls["shared"] == 1
    assert state["lease_active"] is False


def test_relative_cwd_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    graph = here / "g"
    live = _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    shared = [
        "--graph",
        "g",
        "--snapshot",
        "current",
        "--expected-export-revision",
        plan["export_revision"],
        "--export-confirmed",
        "--json",
    ]
    module = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphrag_code.snapshot_export_apply",
            *shared,
            "--destination",
            "copied-module",
        ],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*shared, "--destination", "copied-script", cwd=here)
    cli = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "snapshot-export-apply",
            *shared,
            "--destination",
            "copied-cli",
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
    assert bodies[0]["resolved_snapshot"] == bodies[1]["resolved_snapshot"] == live.name
    assert bodies[0]["observed_export_revision"] == plan["export_revision"]
    assert bodies[0]["files"] == bodies[1]["files"] == bodies[2]["files"]
    assert bodies[0]["destination"] == str((here / "copied-module").resolve())
    assert result_to_json(bodies[0]) == module.stdout
    assert (here / "copied-module").is_dir()
    assert (here / "copied-script").is_dir()
    assert (here / "copied-cli").is_dir()

    dest_installed = here / "copied-wheel"
    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-export-apply",
            "--graph",
            str(graph),
            "--snapshot",
            live.name,
            "--destination",
            str(dest_installed),
            "--expected-export-revision",
            plan["export_revision"],
            "--export-confirmed",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["observed_export_revision"] == plan["export_revision"]

    help_proc = subprocess.run(
        [sys.executable, str(CLI), "snapshot-export-apply", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert help_proc.returncode == 0
    assert "--export-confirmed" in help_proc.stdout
    assert "--expected-export-revision" in help_proc.stdout

    import tarfile

    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_export_apply.py" in names
    assert "_rename_noreplace.py" in names


def test_cli_serializes_writes_and_flushes_under_shared_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "flush-out"
    original_scope = apply_mod._snapshot_export_apply_scope
    original_json = apply_mod.result_to_json
    original_format = apply_mod.format_result
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

    monkeypatch.setattr(apply_mod, "_snapshot_export_apply_scope", tracked_scope)
    monkeypatch.setattr(apply_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(apply_mod, "format_result", guarded_format)
    monkeypatch.setattr(apply_mod.sys, "stdout", GuardedStdout())
    assert (
        apply_mod.main(
            [
                "--graph",
                str(graph),
                "--snapshot",
                "current",
                "--destination",
                str(dest),
                "--expected-export-revision",
                plan["export_revision"],
                "--export-confirmed",
                "--json",
            ]
        )
        == 0
    )
    dest2 = tmp_path / "flush-out-2"
    assert (
        apply_mod.main(
            [
                "--graph",
                str(graph),
                "--snapshot",
                "current",
                "--destination",
                str(dest2),
                "--expected-export-revision",
                plan["export_revision"],
                "--export-confirmed",
            ]
        )
        == 0
    )
    assert state["active"] is False
    assert state["responses"] >= 2
    assert state["flushes"] == 2
    assert dest.is_dir() and dest2.is_dir()


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
    assert "graph_read_lease" in imported
    assert "_held_snapshot_export_plan_unlocked" in imported
    assert "rename_directory_noreplace" in imported
    assert HASH_CHUNK_BYTES <= 64 * 1024
    assert "dir_fd=" in source
    assert "O_EXCL" in source
    assert "O_NOFOLLOW" in source
    assert "read_bytes" not in source
    assert "rmtree" not in source
    assert "graph_exclusive_lease" not in source
    assert "snapshot_export_plan(" not in source
    assert "publish_byog_snapshot" not in source
    native = NATIVE.read_text(encoding="utf-8")
    assert "renameatx_np" in native
    assert "renameat2" in native
    assert "RENAME_EXCL" in native
    assert "os.rename(" not in native
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered


def test_mcp_remains_exactly_fourteen_and_byog_roots_unchanged(tmp_path: Path):
    from anyio import run as anyio_run

    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    assert len(TOOL_NAMES) == 14
    assert "snapshot_export_apply" not in TOOL_NAMES
    assert "snapshot_export_plan" not in TOOL_NAMES
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
            assert "snapshot_export_apply" not in names

    anyio_run(_body)
    dest = tmp_path / "refused"
    refused = _run(
        "--graph",
        str(graph),
        "--snapshot",
        "current",
        "--destination",
        str(dest),
        "--expected-export-revision",
        "sha256:" + ("00" * 32),
        "--export-confirmed",
        "--json",
    )
    assert refused.returncode == 2
    assert refused.stdout == ""
    after = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert after == before


def test_plan_bound_tokens_detect_current_and_listing_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod
    import graphrag_code.snapshot_export_plan as plan_mod

    graph = tmp_path / "g"
    first = _publish(graph, "old")
    live = _publish(graph, "new")
    dest = tmp_path / "token-out"
    plan = snapshot_export_plan(graph, "current")
    other_inodes = {path.stat().st_ino for path in first.iterdir() if path.is_file()}
    original_apply_read = apply_mod.os.read
    original_plan_read = plan_mod.os.read

    def reject_other_snapshot(fd, count):
        assert os.fstat(fd).st_ino not in other_inodes
        return original_apply_read(fd, count)

    def reject_other_plan(fd, count):
        assert os.fstat(fd).st_ino not in other_inodes
        return original_plan_read(fd, count)

    def retarget_and_add(_root, _plan):
        extra = graph / "snapshots" / "19990101-000000-addedone"
        if not extra.exists():
            shutil.copytree(live, extra)
        (graph / "current").write_text(first.name + "\n", encoding="utf-8")

    monkeypatch.setattr(apply_mod, "_after_export_apply_plan_computed", retarget_and_add)
    monkeypatch.setattr(apply_mod.os, "read", reject_other_snapshot)
    monkeypatch.setattr(plan_mod.os, "read", reject_other_plan)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="current|listing|lock"):
        snapshot_export_apply(
            graph,
            "current",
            dest,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dest.exists()
    assert (graph / "current").read_text(encoding="utf-8").strip() == first.name

    # A token change during the final source rehash must also be visible.
    monkeypatch.setattr(apply_mod, "_after_export_apply_plan_computed", lambda *_: None)
    monkeypatch.setattr(apply_mod.os, "read", original_apply_read)
    monkeypatch.setattr(plan_mod.os, "read", original_plan_read)
    graph2 = tmp_path / "g2"
    old2 = _publish(graph2, "old2")
    _publish(graph2, "new2")
    plan2 = snapshot_export_plan(graph2, "current")
    dest2 = tmp_path / "token-during-rehash"
    original_stream = apply_mod._stream_regular_file
    state = {"armed": False, "changed": False}

    def arm_rehash(_root, _records):
        state["armed"] = True

    def retarget_during_rehash(*args, **kwargs):
        if state["armed"] and not state["changed"]:
            state["changed"] = True
            (graph2 / "current").write_text(old2.name + "\n", encoding="utf-8")
        return original_stream(*args, **kwargs)

    monkeypatch.setattr(apply_mod, "_after_export_apply_staged_verified", arm_rehash)
    monkeypatch.setattr(apply_mod, "_stream_regular_file", retarget_during_rehash)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="current|listing|lock"):
        snapshot_export_apply(
            graph2,
            "current",
            dest2,
            plan2["export_revision"],
            export_confirmed=True,
        )
    assert state["changed"] is True
    assert not dest2.exists()


def test_publication_binds_held_staging_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "held-out"
    held: dict[str, tuple[int, int]] = {}

    def remember(_parent_fd, _staging_name, staging_fd):
        info = os.fstat(staging_fd)
        held["inode"] = (info.st_dev, info.st_ino)

    monkeypatch.setattr(apply_mod, "_after_export_apply_staging_created", remember)
    result = snapshot_export_apply(
        graph,
        "current",
        dest,
        plan["export_revision"],
        export_confirmed=True,
    )
    assert result["ok"] is True
    dest_info = dest.lstat()
    assert (dest_info.st_dev, dest_info.st_ino) == held["inode"]
    assert result["destination_verified"] is True


def test_staging_pathname_replacement_before_publication_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "replaced-stage-out"
    holder: dict[str, str] = {}

    def remember(_parent_fd, staging_name, _staging_fd):
        holder["name"] = staging_name

    def replace_staging(parent_fd, _dest_name, staging_name):
        _replace_named_directory(parent_fd, staging_name, b"foreign")

    monkeypatch.setattr(apply_mod, "_after_export_apply_staging_created", remember)
    monkeypatch.setattr(apply_mod, "_before_export_apply_publication", replace_staging)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="staging pathname|changed"):
        snapshot_export_apply(
            graph,
            "current",
            dest,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dest.exists()
    replacement = dest.parent / holder["name"]
    assert (replacement / "witness.bin").read_bytes() == b"foreign"
    assert not (replacement / "entities.parquet").exists()

    # Publishing through an anchored parent fd must not silently move the
    # export outside the canonical parent path reported to the caller.
    monkeypatch.setattr(apply_mod, "_after_export_apply_staging_created", lambda *_: None)
    parent = tmp_path / "canonical-parent"
    parent.mkdir()
    moved_parent = tmp_path / "moved-parent"
    dest2 = parent / "parent-race-out"

    def replace_parent(_parent_fd, _dest_name, _staging_name):
        parent.rename(moved_parent)
        parent.mkdir()
        (parent / "witness.bin").write_bytes(b"replacement-parent")

    monkeypatch.setattr(apply_mod, "_before_export_apply_publication", replace_parent)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="destination parent"):
        snapshot_export_apply(
            graph,
            "current",
            dest2,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dest2.exists()
    assert (parent / "witness.bin").read_bytes() == b"replacement-parent"
    assert _leftover_staging(moved_parent) == []


def test_staged_mutation_after_source_reobserve_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "mutated-stage-out"
    holder: dict[str, Path] = {}

    def remember(_parent_fd, staging_name, _staging_fd):
        holder["path"] = dest.parent / staging_name

    def mutate(_root, _records):
        payload = holder["path"] / "entities.parquet"
        payload.write_bytes(payload.read_bytes() + b"X")

    monkeypatch.setattr(apply_mod, "_after_export_apply_staging_created", remember)
    monkeypatch.setattr(apply_mod, "_after_export_apply_source_reobserved", mutate)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="staged payload|did not match"):
        snapshot_export_apply(
            graph,
            "current",
            dest,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dest.exists()
    assert _leftover_staging(dest.parent) == []


def test_destination_inode_mismatch_after_publish_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "swap-out"
    held: dict[str, tuple[int, int]] = {}

    def remember(_parent_fd, _staging_name, staging_fd):
        info = os.fstat(staging_fd)
        held["inode"] = (info.st_dev, info.st_ino)

    def swap_dest(parent_fd, dest_name):
        _replace_named_directory(parent_fd, dest_name, b"not-the-export")

    monkeypatch.setattr(apply_mod, "_after_export_apply_staging_created", remember)
    monkeypatch.setattr(apply_mod, "_after_export_apply_published", swap_dest)
    result = snapshot_export_apply(
        graph,
        "current",
        dest,
        plan["export_revision"],
        export_confirmed=True,
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["export_performed"] is True
    assert result["destination_created"] is True
    assert result["destination_verified"] is False
    assert result["source_unchanged"] is True
    assert dest.is_dir()
    assert (dest / "witness.bin").read_bytes() == b"not-the-export"
    dest_info = dest.lstat()
    assert (dest_info.st_dev, dest_info.st_ino) != held["inode"]


def test_parent_fsync_failure_after_publish_emits_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    dest = tmp_path / "fsync-parent-out"
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
    result = snapshot_export_apply(
        graph,
        "current",
        dest,
        plan["export_revision"],
        export_confirmed=True,
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["export_performed"] is True
    assert result["destination_created"] is True
    assert result["destination_verified"] is True
    assert result["parent_fsync_confirmed"] is False
    assert dest.is_dir()
    assert (dest / "entities.parquet").is_file()
    text = format_result(result)
    assert "partial=true" in text
    assert "parent_fsync_confirmed=false" in text
    assert "not a backup" in text

    class GuardedStdout:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        def write(self, text: str) -> int:
            self.chunks.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    dest2 = tmp_path / "fsync-parent-cli"
    published["done"] = False
    stdout = GuardedStdout()
    monkeypatch.setattr(apply_mod.sys, "stdout", stdout)
    assert (
        apply_mod.main(
            [
                "--graph",
                str(graph),
                "--snapshot",
                "current",
                "--destination",
                str(dest2),
                "--expected-export-revision",
                plan["export_revision"],
                "--export-confirmed",
                "--json",
            ]
        )
        == 1
    )
    emitted = json.loads("".join(stdout.chunks))
    assert emitted["partial"] is True
    assert dest2.is_dir()

    # Plan-layer verification exceptions after rename must also become an
    # explicit partial result, never an empty-output refusal with a visible
    # destination.
    monkeypatch.setattr(apply_mod.sys, "stdout", sys.__stdout__)
    monkeypatch.setattr(apply_mod, "_fsync_directory", real)
    published["done"] = False
    original_verify = apply_mod._verify_staged_payloads

    def fail_post_publish_verify(*args, **kwargs):
        if published["done"]:
            raise SnapshotExportPlanIntegrityError(
                "injected post-publication plan verification failure"
            )
        return original_verify(*args, **kwargs)

    dest3 = tmp_path / "post-publish-plan-error"
    monkeypatch.setattr(apply_mod, "_verify_staged_payloads", fail_post_publish_verify)
    result3 = snapshot_export_apply(
        graph,
        "current",
        dest3,
        plan["export_revision"],
        export_confirmed=True,
    )
    assert result3["ok"] is False
    assert result3["partial"] is True
    assert result3["destination_created"] is True
    assert result3["destination_verified"] is False
    assert "post-publication plan verification" in result3["error"]
    assert dest3.is_dir()


def test_injected_failures_cleanup_only_owned_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    original_read = apply_mod._read_chunk

    def boom_read(_fd, _size):
        raise OSError(errno.EIO, "injected read failure")

    read_dest = tmp_path / "cleanup-read"
    monkeypatch.setattr(apply_mod, "_read_chunk", boom_read)
    with pytest.raises(SnapshotExportApplyError, match="read export payload"):
        snapshot_export_apply(
            graph,
            "current",
            read_dest,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not read_dest.exists()
    assert _leftover_staging(read_dest.parent) == []
    monkeypatch.setattr(apply_mod, "_read_chunk", original_read)

    def boom_file_fsync(_fd):
        raise SnapshotExportApplyError("fsync export payload failed: injected file fsync")

    file_dest = tmp_path / "cleanup-file-fsync"
    original_file_fsync = apply_mod._fsync_file
    monkeypatch.setattr(apply_mod, "_fsync_file", boom_file_fsync)
    with pytest.raises(SnapshotExportApplyError, match="fsync export payload"):
        snapshot_export_apply(
            graph,
            "current",
            file_dest,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not file_dest.exists()
    assert _leftover_staging(file_dest.parent) == []
    monkeypatch.setattr(apply_mod, "_fsync_file", original_file_fsync)

    real_dir_fsync = apply_mod._fsync_directory

    def boom_dir_fsync(fd):
        raise SnapshotExportApplyError(
            "fsync export directory failed: injected staging fsync"
        )

    dir_dest = tmp_path / "cleanup-dir-fsync"
    monkeypatch.setattr(apply_mod, "_fsync_directory", boom_dir_fsync)
    with pytest.raises(SnapshotExportApplyError, match="fsync export directory"):
        snapshot_export_apply(
            graph,
            "current",
            dir_dest,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dir_dest.exists()
    assert _leftover_staging(dir_dest.parent) == []
    monkeypatch.setattr(apply_mod, "_fsync_directory", real_dir_fsync)

    def corrupt(_root, _records):
        leftovers = _leftover_staging(verify_dest.parent)
        assert leftovers
        payload = leftovers[0] / "entities.parquet"
        payload.write_bytes(payload.read_bytes() + b"X")

    verify_dest = tmp_path / "cleanup-verify"
    monkeypatch.setattr(apply_mod, "_after_export_apply_copied", corrupt)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="staged payload|did not match"):
        snapshot_export_apply(
            graph,
            "current",
            verify_dest,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not verify_dest.exists()
    assert _leftover_staging(verify_dest.parent) == []


def test_replaced_staging_and_witness_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_apply as apply_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_export_plan(graph, "current")
    holder: dict[str, str] = {}

    def remember_mkdir(_parent_fd, staging_name):
        holder["name"] = staging_name

    def replace_before_open(parent_fd, staging_name):
        remember_mkdir(parent_fd, staging_name)
        _replace_named_directory(parent_fd, staging_name, b"before-open")

    dest1 = tmp_path / "replace-before-open"
    monkeypatch.setattr(apply_mod, "_after_export_apply_staging_mkdir", replace_before_open)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="staging directory"):
        snapshot_export_apply(
            graph,
            "current",
            dest1,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dest1.exists()
    assert (dest1.parent / holder["name"] / "witness.bin").read_bytes() == b"before-open"

    def replace_after_open(parent_fd, staging_name, _staging_fd):
        holder["name"] = staging_name
        _replace_named_directory(parent_fd, staging_name, b"after-open")

    dest2 = tmp_path / "replace-after-open"
    monkeypatch.setattr(apply_mod, "_after_export_apply_staging_mkdir", lambda *_: None)
    monkeypatch.setattr(apply_mod, "_after_export_apply_staging_opened", replace_after_open)
    with pytest.raises(SnapshotExportApplyIntegrityError, match="staging directory"):
        snapshot_export_apply(
            graph,
            "current",
            dest2,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dest2.exists()
    assert (dest2.parent / holder["name"] / "witness.bin").read_bytes() == b"after-open"

    def boom_write(_fd, _data):
        raise OSError(errno.EIO, "injected write failure")

    def replace_during_cleanup(parent_fd, staging_name):
        holder["name"] = staging_name
        _replace_named_directory(parent_fd, staging_name, b"during-cleanup")

    dest3 = tmp_path / "replace-during-cleanup"
    monkeypatch.setattr(apply_mod, "_after_export_apply_staging_opened", lambda *_: None)
    monkeypatch.setattr(apply_mod, "_write_chunk", boom_write)
    monkeypatch.setattr(
        apply_mod, "_before_export_apply_staging_cleanup", replace_during_cleanup
    )
    with pytest.raises(SnapshotExportApplyError, match="write export payload"):
        snapshot_export_apply(
            graph,
            "current",
            dest3,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dest3.exists()
    assert (dest3.parent / holder["name"] / "witness.bin").read_bytes() == b"during-cleanup"

    def replace_before_rmdir(parent_fd, staging_name):
        holder["name"] = staging_name
        _replace_named_directory(parent_fd, staging_name, b"before-rmdir")

    dest4 = tmp_path / "replace-before-rmdir"
    monkeypatch.setattr(apply_mod, "_before_export_apply_staging_cleanup", lambda *_: None)
    monkeypatch.setattr(apply_mod, "_before_export_apply_staging_rmdir", replace_before_rmdir)
    with pytest.raises(SnapshotExportApplyError, match="write export payload"):
        snapshot_export_apply(
            graph,
            "current",
            dest4,
            plan["export_revision"],
            export_confirmed=True,
        )
    assert not dest4.exists()
    assert (dest4.parent / holder["name"] / "witness.bin").read_bytes() == b"before-rmdir"
