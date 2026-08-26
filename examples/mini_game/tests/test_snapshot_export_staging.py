"""Read-only snapshot-export-apply staging inventory.

Disposable tmp parents only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_export_staging.py -q
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_export_staging import (  # type: ignore
    MAX_PARENT_ENTRIES,
    MAX_PREFIXED_ENTRIES,
    STAGING_NAME_PREFIX,
    SnapshotExportStagingError,
    SnapshotExportStagingIntegrityError,
    format_result,
    inventory_revision_of,
    result_to_json,
    snapshot_export_staging,
)

SCRIPT = ROOT / "scripts" / "snapshot_export_staging.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_staging.py"
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
        "snapshot_export_reconcile",
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
        "unlink",
        "rmdir",
        "rmtree",
        "rename",
        "replace",
        "mkdir",
        "read_bytes",
        "write_bytes",
        "readlink",
    }
)
FORBIDDEN_WORDS = ("orphaned", "abandoned", "stale", "expired", "safe to delete")
HEX_A = "a" * 32
HEX_B = "b" * 32
HEX_C = "c" * 32


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    src = str(ROOT / "src")
    current = env.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if src not in parts:
        env["PYTHONPATH"] = src + ((os.pathsep + current) if current else "")
    return env


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


def _staging_name(suffix: str) -> str:
    return f"{STAGING_NAME_PREFIX}{suffix}"


def _make_staging(parent: Path, suffix: str) -> Path:
    path = parent / _staging_name(suffix)
    path.mkdir()
    return path


def _notice_codes(result: dict) -> list[str]:
    return [notice["code"] for notice in result["notices"]]


def _assert_inventory_shape(result: dict, parent: Path) -> None:
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["parent"] == str(parent.resolve())
    assert result["staging_count"] == len(result["staging_entries"])
    assert result["unrecognized_prefixed_count"] == len(
        result["unrecognized_prefixed_entries"]
    )
    assert result["unsafe_staging_count"] == sum(
        1 for entry in result["staging_entries"] if entry["kind"] != "directory"
    )
    assert result["ownership_known"] is False
    assert result["writer_activity_known"] is False
    assert result["cleanup_supported"] is False
    assert result["cleanup_performed"] is False
    assert result["parent_mutated"] is False
    assert result["graph_inspected"] is False
    assert result["inventory_revision"] == inventory_revision_of(result)
    assert result["inventory_revision"].startswith("sha256:")
    assert len(result["inventory_revision"]) == len("sha256:") + 64
    assert _notice_codes(result) == [
        "name_match_is_not_creation_proof",
        "stable_observation_is_not_writer_absence",
        "absence_does_not_prove_cleanup",
        "inventory_performs_no_cleanup",
        "contents_not_inspected",
        "writer_lease_is_not_activity",
        "not_backup_or_authenticity",
        "observation_window_only",
        "cli_only_not_mcp",
    ]
    names = [entry["name"] for entry in result["staging_entries"]]
    assert names == sorted(names, key=lambda item: item.encode("utf-8"))
    unrec = [entry["name"] for entry in result["unrecognized_prefixed_entries"]]
    assert unrec == sorted(unrec, key=lambda item: item.encode("utf-8"))
    text = format_result(result)
    assert str(parent.resolve()) in text
    assert "inventory-only" in text
    assert "not authorization to delete" in text
    assert "backup" not in text.lower()
    assert "recoverable" not in text.lower()
    assert "authentic" not in text.lower()


def _assert_entry_nonclaims(entry: dict, *, matches: bool) -> None:
    assert entry["name_matches_current_protocol"] is matches
    assert entry["ownership"] == "unknown"
    assert entry["writer_activity"] == "unknown"
    assert entry["cleanup_eligible"] is False
    assert entry["contents_inspected"] is False
    for key in ("dev", "ino", "mode", "size", "mtime_ns", "ctime_ns"):
        assert isinstance(entry[key], int)
    if matches and entry["kind"] == "directory":
        assert entry["writer_lease_state"] in {
            "metadata_absent",
            "metadata_unsafe",
            "held_at_scan",
            "not_held_at_scan",
        }
        assert entry["writer_lease_metadata_present"] is (
            entry["writer_lease_state"] != "metadata_absent"
        )
        assert entry["writer_lease_contended"] is (
            entry["writer_lease_state"] == "held_at_scan"
        )
        assert entry["writer_lease_path"].endswith("/.export-writer.lock")
        if entry["writer_lease_state"] == "metadata_absent":
            for key in (
                "writer_lease_dev",
                "writer_lease_ino",
                "writer_lease_mode",
                "writer_lease_size",
                "writer_lease_mtime_ns",
                "writer_lease_ctime_ns",
            ):
                assert entry[key] is None
        else:
            for key in (
                "writer_lease_dev",
                "writer_lease_ino",
                "writer_lease_mode",
                "writer_lease_size",
                "writer_lease_mtime_ns",
                "writer_lease_ctime_ns",
            ):
                assert isinstance(entry[key], int)
    else:
        assert "writer_lease_state" not in entry


def test_empty_parent(tmp_path: Path):
    parent = tmp_path / "empty"
    parent.mkdir()
    result = snapshot_export_staging(parent)
    _assert_inventory_shape(result, parent)
    assert result["staging_entries"] == []
    assert result["staging_count"] == 0
    assert result["unsafe_staging_count"] == 0
    assert result["unrecognized_prefixed_entries"] == []
    assert result["unrecognized_prefixed_count"] == 0
    assert result["other_entry_count"] == 0


def test_one_and_multiple_protocol_directories_utf8_order(tmp_path: Path):
    parent = tmp_path / "multi"
    parent.mkdir()
    later = _make_staging(parent, HEX_C)
    earlier = _make_staging(parent, HEX_A)
    mid = _make_staging(parent, HEX_B)
    one = snapshot_export_staging(parent)
    _assert_inventory_shape(one, parent)
    assert one["staging_count"] == 3
    assert [entry["name"] for entry in one["staging_entries"]] == sorted(
        [later.name, earlier.name, mid.name], key=lambda item: item.encode("utf-8")
    )
    for entry in one["staging_entries"]:
        assert entry["kind"] == "directory"
        _assert_entry_nonclaims(entry, matches=True)
    assert one["unsafe_staging_count"] == 0


def test_near_miss_names_are_unrecognized_or_other(tmp_path: Path):
    import graphrag_code.snapshot_export_staging as staging_mod

    parent = tmp_path / "nearmiss"
    parent.mkdir()
    recognized = _make_staging(parent, HEX_A)
    near_misses = {
        _staging_name("a" * 31): "short",
        _staging_name("a" * 33): "long",
        _staging_name("B" * 32): "upper",
        _staging_name("g" * 32): "nonhex",
        STAGING_NAME_PREFIX: "empty",
        _staging_name(HEX_B + "-extra"): "extra",
        _staging_name(HEX_B + ".dir"): "dotted",
    }
    for name in near_misses:
        (parent / name).mkdir()
    other_names = {
        ".graphrag-export": "missing-hyphen-suffix",
        f"graphrag-export-{HEX_A}": "missing-dot",
        "unrelated.txt": "plain",
    }
    (parent / ".graphrag-export").mkdir()
    (parent / f"graphrag-export-{HEX_A}").mkdir()
    (parent / "unrelated.txt").write_text("x", encoding="utf-8")
    result = snapshot_export_staging(parent)
    _assert_inventory_shape(result, parent)
    assert [entry["name"] for entry in result["staging_entries"]] == [recognized.name]
    unrec_names = [entry["name"] for entry in result["unrecognized_prefixed_entries"]]
    assert unrec_names == sorted(near_misses, key=lambda item: item.encode("utf-8"))
    for entry in result["unrecognized_prefixed_entries"]:
        _assert_entry_nonclaims(entry, matches=False)
        assert entry["kind"] == "directory"
    assert result["unrecognized_prefixed_count"] == len(near_misses)
    assert result["other_entry_count"] == len(other_names)
    reported = {entry["name"] for entry in result["staging_entries"]} | {
        entry["name"] for entry in result["unrecognized_prefixed_entries"]
    }
    assert reported.isdisjoint(other_names)
    # POSIX exposes undecodable names through surrogateescape. They must be
    # sortable as raw filesystem bytes rather than crashing a parent scan.
    assert staging_mod._name_sort_key("unrelated-\udcff") == b"unrelated-\xff"


def test_exact_name_file_symlink_and_other_types_never_follow(tmp_path: Path):
    parent = tmp_path / "kinds"
    parent.mkdir()
    outside = tmp_path / "outside-target"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_bytes(b"must-not-follow")
    file_entry = parent / _staging_name(HEX_A)
    file_entry.write_bytes(b"not-a-directory")
    linked = parent / _staging_name(HEX_B)
    linked.symlink_to(outside, target_is_directory=True)
    fifo = parent / _staging_name(HEX_C)
    os.mkfifo(fifo)
    before = secret.stat()
    result = snapshot_export_staging(parent)
    _assert_inventory_shape(result, parent)
    kinds = {entry["name"]: entry["kind"] for entry in result["staging_entries"]}
    assert kinds[file_entry.name] == "file"
    assert kinds[linked.name] == "symlink"
    assert kinds[fifo.name] == "fifo"
    assert result["staging_count"] == 3
    assert result["unsafe_staging_count"] == 3
    dumped = result_to_json(result)
    assert "must-not-follow" not in dumped
    assert secret.read_bytes() == b"must-not-follow"
    assert secret.stat().st_mtime_ns == before.st_mtime_ns
    assert linked.is_symlink()
    assert not any(entry.get("target") for entry in result["staging_entries"])


def test_unrelated_children_affect_only_other_entry_count(tmp_path: Path):
    parent = tmp_path / "other"
    parent.mkdir()
    staging = _make_staging(parent, HEX_A)
    (parent / "notes.txt").write_text("keep", encoding="utf-8")
    (parent / "current").write_text("not-a-graph-read", encoding="utf-8")
    (parent / "snapshots").mkdir()
    (parent / ".publish.lock").write_bytes(b"lock")
    result = snapshot_export_staging(parent)
    _assert_inventory_shape(result, parent)
    assert [entry["name"] for entry in result["staging_entries"]] == [staging.name]
    assert result["other_entry_count"] == 4
    assert result["unrecognized_prefixed_count"] == 0
    dumped = result_to_json(result)
    assert '"notes.txt"' not in dumped
    assert '"current"' not in dumped
    assert '"snapshots"' not in dumped
    assert '".publish.lock"' not in dumped


def test_total_and_prefixed_entry_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import graphrag_code.snapshot_export_staging as staging_mod

    parent = tmp_path / "bounds"
    parent.mkdir()
    _make_staging(parent, HEX_A)
    (parent / "extra.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(staging_mod, "MAX_PARENT_ENTRIES", 1)
    with pytest.raises(SnapshotExportStagingError, match="parent entry count"):
        snapshot_export_staging(parent)
    monkeypatch.setattr(staging_mod, "MAX_PARENT_ENTRIES", MAX_PARENT_ENTRIES)

    two = tmp_path / "two-prefixed"
    two.mkdir()
    _make_staging(two, HEX_A)
    _make_staging(two, HEX_B)
    monkeypatch.setattr(staging_mod, "MAX_PREFIXED_ENTRIES", 1)
    with pytest.raises(SnapshotExportStagingError, match="prefixed export-staging"):
        snapshot_export_staging(two)
    monkeypatch.setattr(staging_mod, "MAX_PREFIXED_ENTRIES", MAX_PREFIXED_ENTRIES)

    wide = tmp_path / "prefixed"
    wide.mkdir()
    for index in range(MAX_PREFIXED_ENTRIES + 1):
        (wide / _staging_name(f"{index:032x}")).mkdir()
    too_many = _run("--parent", str(wide), "--json")
    assert too_many.returncode == 2
    assert too_many.stdout == ""
    assert "prefixed export-staging entry count" in too_many.stderr

    monkeypatch.setattr(staging_mod.os, "supports_fd", set())
    with pytest.raises(SnapshotExportStagingError, match="descriptor-relative"):
        snapshot_export_staging(parent)


def test_parent_symlink_non_directory_missing(tmp_path: Path):
    missing = tmp_path / "missing"
    proc = _run("--parent", str(missing), "--json")
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "does not exist" in proc.stderr

    file_parent = tmp_path / "file-parent"
    file_parent.write_text("nope", encoding="utf-8")
    file_proc = _run("--parent", str(file_parent), "--json")
    assert file_proc.returncode == 2
    assert file_proc.stdout == ""
    assert "not a real directory" in file_proc.stderr

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    link_proc = _run("--parent", str(linked), "--json")
    assert link_proc.returncode == 2
    assert link_proc.stdout == ""
    assert "symlink" in link_proc.stderr.lower()


def test_parent_replacement_before_open_never_reads_symlink_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod

    parent = tmp_path / "preanchor"
    parent.mkdir()
    _make_staging(parent, HEX_A)
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    planted = _make_staging(replacement, HEX_B)
    (planted / "payload.bin").write_bytes(b"must-not-read")
    hidden = tmp_path / "hidden-original"

    def replace_before_open(path):
        if path == parent and parent.exists() and not parent.is_symlink():
            parent.rename(hidden)
            parent.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(staging_mod, "_after_parent_path_inspected", replace_before_open)
    with pytest.raises(
        SnapshotExportStagingIntegrityError, match="changed|symlink|unsafe"
    ):
        snapshot_export_staging(parent)
    assert (planted / "payload.bin").read_bytes() == b"must-not-read"
    assert parent.is_symlink()
    assert (hidden / _staging_name(HEX_A)).is_dir()


def test_parent_replacement_after_anchor_and_rename_away_and_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod

    parent = tmp_path / "held"
    parent.mkdir()
    original = _make_staging(parent, HEX_A)
    replacement = tmp_path / "after-replacement"
    replacement.mkdir()
    planted = _make_staging(replacement, HEX_B)
    (planted / "payload.bin").write_bytes(b"after-target")
    hidden = tmp_path / "held-hidden"

    def replace_after_open(path, parent_fd):
        if path == parent.resolve():
            os.fstat(parent_fd)
            parent.rename(hidden)
            parent.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(staging_mod, "_after_parent_opened", replace_after_open)
    with pytest.raises(
        SnapshotExportStagingIntegrityError, match="changed|symlink|unsafe"
    ):
        snapshot_export_staging(parent)
    assert (planted / "payload.bin").read_bytes() == b"after-target"
    assert (hidden / original.name).is_dir()
    assert parent.is_symlink()

    monkeypatch.setattr(staging_mod, "_after_parent_opened", lambda *_: None)
    bounced = tmp_path / "bounced"
    bounced.mkdir()
    _make_staging(bounced, HEX_A)
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
        SnapshotExportStagingIntegrityError, match="parent identity|parent changed"
    ):
        snapshot_export_staging(bounced)


def test_child_replacement_and_metadata_and_directory_token_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod

    parent = tmp_path / "kids"
    parent.mkdir()
    child = _make_staging(parent, HEX_A)

    parked = tmp_path / "parked-child"

    def replace_child(_path, _scan):
        child.rename(parked)
        child.mkdir()

    monkeypatch.setattr(staging_mod, "_after_first_scan", replace_child)
    with pytest.raises(
        SnapshotExportStagingIntegrityError,
        match="entry identity|parent identity|parent changed|listing",
    ):
        snapshot_export_staging(parent)
    child.rmdir()
    parked.rename(child)

    def chmod_child(_path, _scan):
        child.chmod(0o705)

    monkeypatch.setattr(staging_mod, "_after_first_scan", chmod_child)
    with pytest.raises(
        SnapshotExportStagingIntegrityError, match="entry metadata|parent identity"
    ):
        snapshot_export_staging(parent)
    child.chmod(0o755)

    def chmod_away_and_back(_path, _scan):
        child.chmod(0o705)
        child.chmod(0o755)

    monkeypatch.setattr(staging_mod, "_after_first_scan", chmod_away_and_back)
    with pytest.raises(
        SnapshotExportStagingIntegrityError, match="entry metadata|parent identity"
    ):
        snapshot_export_staging(parent)

    def mutate_contents(_path, _scan):
        (child / "inside.bin").write_bytes(b"changed-directory-token")

    monkeypatch.setattr(staging_mod, "_after_first_scan", mutate_contents)
    with pytest.raises(
        SnapshotExportStagingIntegrityError, match="entry metadata|parent identity"
    ):
        snapshot_export_staging(parent)


def test_deterministic_inventory_revision_and_json(tmp_path: Path):
    parent = tmp_path / "stable"
    parent.mkdir()
    first_dir = _make_staging(parent, HEX_B)
    second_dir = _make_staging(parent, HEX_A)
    (parent / "notes.txt").write_text("other", encoding="utf-8")
    (parent / _staging_name("d" * 31)).mkdir()
    first = snapshot_export_staging(parent)
    second = snapshot_export_staging(parent)
    _assert_inventory_shape(first, parent)
    assert first == second
    assert result_to_json(first) == result_to_json(second)
    assert [entry["name"] for entry in first["staging_entries"]] == [
        first_dir.name if first_dir.name.encode("utf-8") < second_dir.name.encode("utf-8")
        else second_dir.name,
        second_dir.name if first_dir.name.encode("utf-8") < second_dir.name.encode("utf-8")
        else first_dir.name,
    ]
    rebuilt = {
        key: first[key]
        for key in (
            "schema_version",
            "staging_entries",
            "staging_count",
            "unsafe_staging_count",
            "unrecognized_prefixed_entries",
            "unrecognized_prefixed_count",
            "other_entry_count",
        )
    }
    assert first["inventory_revision"] == inventory_revision_of(rebuilt)


def test_relative_cwd_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    parent = here / "parent"
    parent.mkdir()
    staging = _make_staging(parent, HEX_A)
    args = ["--parent", "parent", "--json"]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_export_staging", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-export-staging", *args],
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
    assert bodies[0]["staging_entries"][0]["name"] == staging.name
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-export-staging",
            "--parent",
            str(parent),
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["inventory_revision"] == bodies[0]["inventory_revision"]

    import tarfile

    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_export_staging.py" in names


def test_descriptor_lifetime_through_serialization_write_flush(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_staging as staging_mod

    parent = tmp_path / "held-fd"
    parent.mkdir()
    _make_staging(parent, HEX_A)
    original_json = staging_mod.result_to_json
    original_format = staging_mod.format_result
    state = {"parent_fd": None, "responses": 0, "flushes": 0}

    def capture_ready(_path, parent_fd, _result):
        state["parent_fd"] = parent_fd
        os.fstat(parent_fd)

    def guarded_json(*args, **kwargs):
        os.fstat(state["parent_fd"])
        return original_json(*args, **kwargs)

    def guarded_format(*args, **kwargs):
        os.fstat(state["parent_fd"])
        return original_format(*args, **kwargs)

    class GuardedStdout:
        def write(self, text: str) -> int:
            os.fstat(state["parent_fd"])
            state["responses"] += 1
            return len(text)

        def flush(self) -> None:
            os.fstat(state["parent_fd"])
            state["flushes"] += 1

    monkeypatch.setattr(staging_mod, "_after_result_ready", capture_ready)
    monkeypatch.setattr(staging_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(staging_mod, "format_result", guarded_format)
    monkeypatch.setattr(staging_mod.sys, "stdout", GuardedStdout())
    assert staging_mod.main(["--parent", str(parent), "--json"]) == 0
    assert staging_mod.main(["--parent", str(parent)]) == 0
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
    assert "graph_read_lease" not in source
    assert "graph_exclusive_lease" not in source
    assert "snapshot_export_apply(" not in source
    assert "snapshot_export_plan(" not in source
    assert "snapshot_export_verify(" not in source
    assert "snapshot_export_reconcile(" not in source
    assert "probe_staging_writer_lease" not in source
    assert "acquire_export_writer_lease" not in source
    assert "publish_byog_snapshot" not in source
    assert "read_bytes" not in source
    assert "write_bytes" not in source
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    human = format_result(
        {
            "parent": "/tmp/x",
            "staging_count": 0,
            "unrecognized_prefixed_count": 0,
            "other_entry_count": 0,
            "inventory_revision": "sha256:" + ("00" * 32),
            "staging_entries": [],
            "ok": True,
        }
    )
    assert "inventory-only" in human
    assert "recoverable" not in human.lower()
    assert "authentic" not in human.lower()
    assert "backup" not in human.lower()


def test_mcp_remains_exactly_thirteen_and_byog_roots_unchanged(tmp_path: Path):
    from anyio import run as anyio_run
    from byog_graph import publish_byog_snapshot  # type: ignore
    import pandas as pd

    before = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(before) == 15
    assert len(TOOL_NAMES) == 13
    assert "snapshot_export_staging" not in TOOL_NAMES
    graph = tmp_path / "g"
    publish_byog_snapshot(
        pd.DataFrame(
            [
                {
                    "id": "ent:a",
                    "title": "demo:a",
                    "type": "function",
                    "source_file": "a.py",
                    "extractor": "tree-sitter-python",
                    "description": "desc-a",
                }
            ]
        ),
        pd.DataFrame(
            [
                {
                    "id": "rel:a",
                    "source": "demo:a.py",
                    "target": "demo:a",
                    "type": "contains",
                    "extractor": "tree-sitter-python",
                }
            ]
        ),
        pd.DataFrame(
            [
                {
                    "id": "tu:a",
                    "title": "a.py",
                    "source_file": "a.py",
                    "entity_id": "ent:a",
                }
            ]
        ),
        graph,
        settings_text="export-staging: a\n",
        keep_last=10,
    )
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 13
            assert "snapshot_export_staging" not in names

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
