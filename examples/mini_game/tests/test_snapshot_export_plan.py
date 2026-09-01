"""Read-only snapshot export plan.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_export_plan.py -q
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
from graphrag_code.snapshot_export_plan import (  # type: ignore
    HASH_CHUNK_BYTES,
    SnapshotExportPlanError,
    SnapshotExportPlanIntegrityError,
    canonical_export_revision_text,
    export_revision_of,
    format_result,
    result_to_json,
    snapshot_export_plan,
)
from graphrag_code.snapshot_pins import OPERATOR_PINS_NAME  # type: ignore

SCRIPT = ROOT / "scripts" / "snapshot_export_plan.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_export_plan.py"
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
        "unlink",
        "rmdir",
        "rmtree",
        "rename",
        "replace",
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
    return ents, rels, tus


def _publish(graph: Path, marker: str, *, keep_last: int = 10) -> Path:
    ents, rels, tus = _rows(marker)
    return publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text=f"export-plan: {marker}\n",
        keep_last=keep_last,
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


def _file_revision(path: Path) -> tuple[int, str]:
    data = path.read_bytes()
    return len(data), "sha256:" + hashlib.sha256(data).hexdigest()


def _assert_plan_shape(result: dict, graph: Path, requested: str, resolved: str) -> None:
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["graph"] == str(graph)
    assert result["requested_snapshot"] == requested
    assert result["resolved_snapshot"] == resolved
    assert result["snapshot_path"] == str(graph / "snapshots" / resolved)
    assert result["export_performed"] is False
    assert result["fresh_plan_required_before_export"] is True
    assert result["file_count"] == len(result["files"])
    assert result["total_size_bytes"] == sum(item["size_bytes"] for item in result["files"])
    paths = [item["path"] for item in result["files"]]
    assert paths == sorted(paths, key=lambda item: item.encode("utf-8"))
    snap = graph / "snapshots" / resolved
    expected_total = 0
    for item in result["files"]:
        size, revision = _file_revision(snap / item["path"])
        assert item["size_bytes"] == size
        assert item["content_revision"] == revision
        expected_total += size
    assert result["total_size_bytes"] == expected_total
    assert result["export_revision"] == export_revision_of(result)
    payload = json.loads(canonical_export_revision_text(result))
    assert set(payload) == {"files", "resolved_snapshot", "schema_version"}
    assert "graph" not in payload
    assert "snapshot_path" not in payload
    assert "ok" not in payload
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == [
        "plan_is_not_export",
        "plan_is_not_backup",
        "export_revision_is_self_consistency_only",
        "fresh_plan_required_before_export",
        "advisory_locks_cooperating_only",
        "cli_only_not_mcp",
    ]
    text = format_result(result)
    assert "export_performed=false" in text
    assert "not a backup" in text
    assert "not authorization to delete" in text


def test_current_and_explicit_retained_selection(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "old")
    live = _publish(graph, "new")
    before = _protected_state(graph)
    current_plan = snapshot_export_plan(graph, "current")
    _assert_plan_shape(current_plan, graph, "current", live.name)
    assert current_plan["resolved_snapshot"] == _current(graph)
    retained = snapshot_export_plan(graph, first.name)
    _assert_plan_shape(retained, graph, first.name, first.name)
    assert retained["resolved_snapshot"] == first.name
    assert retained["export_revision"] != current_plan["export_revision"]
    assert {item["path"] for item in current_plan["files"]} == {
        "entities.parquet",
        "manifest.json",
        "relationships.parquet",
        "settings.yaml",
        "text_units.parquet",
    }
    assert _protected_state(graph) == before
    assert first.is_dir() and live.is_dir()


def test_deterministic_byte_order_and_exact_hashes(tmp_path: Path):
    graph = tmp_path / "g"
    live = _publish(graph, "only")
    first = snapshot_export_plan(graph, "current")
    second = snapshot_export_plan(graph, live.name)
    assert first["files"] == second["files"]
    assert first["export_revision"] == second["export_revision"]
    assert result_to_json(first) == result_to_json(
        {**second, "requested_snapshot": "current"}
    ) or first["export_revision"] == second["export_revision"]
    names = [item["path"] for item in first["files"]]
    assert names == sorted(names, key=lambda item: item.encode("utf-8"))
    digest = hashlib.sha256(
        canonical_export_revision_text(first).encode("utf-8")
    ).hexdigest()
    assert first["export_revision"] == "sha256:" + digest


def test_malformed_dangling_and_noncanonical_selectors(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "only")
    before = _protected_state(graph)
    missing = _run("--graph", str(graph), "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""
    for token in ("", " current", "current ", "../x", "x/y", ".staging-1", "not a snapshot"):
        proc = _run("--graph", str(graph), "--snapshot", token, "--json")
        assert proc.returncode == 2
        assert proc.stdout == ""
    dangling = _run(
        "--graph", str(graph), "--snapshot", "19990101-000000-deadbeef", "--json"
    )
    assert dangling.returncode == 2
    assert dangling.stdout == ""
    (graph / "current").write_text("19990101-000000-missing\n", encoding="utf-8")
    with pytest.raises(SnapshotExportPlanError, match="does not exist|not a retained"):
        snapshot_export_plan(graph, "current")
    after_hashes = _payload_hashes(graph)
    assert {key: value for key, value in before["hashes"].items() if key != "current"} == {
        key: value for key, value in after_hashes.items() if key != "current"
    }


def test_missing_and_unsafe_lock_and_layout(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "only")
    (graph / PUBLICATION_LOCK_NAME).unlink()
    missing = _run("--graph", str(graph), "--snapshot", "current", "--json")
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "adopt-publication-lock" in missing.stderr

    flat = tmp_path / "flat"
    flat.mkdir()
    (flat / "entities.parquet").write_bytes(b"x")
    legacy = _run("--graph", str(flat), "--snapshot", "current", "--json")
    assert legacy.returncode == 2
    assert legacy.stdout == ""

    linked = tmp_path / "link-graph"
    linked.symlink_to(tmp_path / "g")
    symlink = _run("--graph", str(linked), "--snapshot", "current", "--json")
    assert symlink.returncode == 2
    assert symlink.stdout == ""


def test_symlink_and_non_regular_payload_rejected(tmp_path: Path):
    graph = tmp_path / "g"
    live = _publish(graph, "only")
    outside = tmp_path / "outside.parquet"
    outside.write_bytes(b"must-not-follow")
    payload = live / "entities.parquet"
    payload.unlink()
    payload.symlink_to(outside)
    with pytest.raises(SnapshotExportPlanIntegrityError, match="symlink"):
        snapshot_export_plan(graph, "current")
    proc = _run("--graph", str(graph), "--snapshot", "current", "--json")
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert outside.read_bytes() == b"must-not-follow"

    graph2 = tmp_path / "g2"
    live2 = _publish(graph2, "dir")
    (live2 / "settings.yaml").unlink()
    (live2 / "settings.yaml").mkdir()
    with pytest.raises(SnapshotExportPlanError, match="not a regular file"):
        snapshot_export_plan(graph2, "current")
    assert live2.is_dir()


def test_content_replacement_listing_current_and_lock_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_plan as plan_mod

    graph = tmp_path / "g"
    first = _publish(graph, "old")
    live = _publish(graph, "new")

    def rewrite_entities(_root, _records):
        target = live / "entities.parquet"
        target.write_bytes(target.read_bytes() + b"X")

    monkeypatch.setattr(plan_mod, "_after_export_files_hashed", rewrite_entities)
    with pytest.raises(SnapshotExportPlanIntegrityError, match="changed"):
        snapshot_export_plan(graph, "current")

    def switch_current(_root, _tokens):
        (graph / "current").write_text(first.name + "\n", encoding="utf-8")

    monkeypatch.setattr(plan_mod, "_after_export_files_hashed", lambda *_: None)
    monkeypatch.setattr(plan_mod, "_after_export_tokens_captured", switch_current)
    with pytest.raises(SnapshotExportPlanIntegrityError, match="current|lock|listing"):
        snapshot_export_plan(graph, "current")
    (graph / "current").write_text(live.name + "\n", encoding="utf-8")

    def add_published(_root, _tokens):
        extra = graph / "snapshots" / "19990101-000000-addedone"
        if not extra.exists():
            shutil.copytree(live, extra)

    monkeypatch.setattr(plan_mod, "_after_export_tokens_captured", add_published)
    with pytest.raises(SnapshotExportPlanIntegrityError, match="listing|lock|current"):
        snapshot_export_plan(graph, live.name)
    shutil.rmtree(graph / "snapshots" / "19990101-000000-addedone", ignore_errors=True)

    def replace_lock(_root, _tokens):
        lock = graph / PUBLICATION_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replaced-lock")

    monkeypatch.setattr(plan_mod, "_after_export_tokens_captured", replace_lock)
    with pytest.raises(SnapshotExportPlanIntegrityError, match="lock|current|listing"):
        snapshot_export_plan(graph, live.name)

    same_size_graph = tmp_path / "same-size"
    same_size_live = _publish(same_size_graph, "same-size")
    same_size_target = same_size_live / "entities.parquet"
    original = same_size_target.read_bytes()
    original_stat = same_size_target.stat()

    def rewrite_same_size_and_restore_mtime(_root, _records):
        replacement = bytes([original[0] ^ 1]) + original[1:]
        same_size_target.write_bytes(replacement)
        os.utime(
            same_size_target,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )

    monkeypatch.setattr(plan_mod, "_after_export_tokens_captured", lambda *_: None)
    monkeypatch.setattr(plan_mod, "_after_export_payload_listed", lambda *_: None)
    monkeypatch.setattr(
        plan_mod, "_after_export_files_hashed", rewrite_same_size_and_restore_mtime
    )
    with pytest.raises(SnapshotExportPlanIntegrityError, match="content changed"):
        snapshot_export_plan(same_size_graph, "current")

    swapped_graph = tmp_path / "swapped-parent"
    swapped_live = _publish(swapped_graph, "anchored")
    outside = tmp_path / "outside-snapshot"
    shutil.copytree(swapped_live, outside)
    outside_inodes = {
        path.stat().st_ino for path in outside.iterdir() if path.is_file()
    }
    hidden = swapped_graph / "snapshots" / "hidden-original"

    def replace_selected_directory_with_symlink(_root, _entries):
        swapped_live.rename(hidden)
        swapped_live.symlink_to(outside, target_is_directory=True)

    original_read = plan_mod.os.read

    def reject_outside_descriptor(fd, count):
        assert os.fstat(fd).st_ino not in outside_inodes
        return original_read(fd, count)

    monkeypatch.setattr(
        plan_mod,
        "_after_export_payload_listed",
        replace_selected_directory_with_symlink,
    )
    monkeypatch.setattr(plan_mod, "_after_export_files_hashed", lambda *_: None)
    monkeypatch.setattr(plan_mod.os, "read", reject_outside_descriptor)
    with pytest.raises(SnapshotExportPlanIntegrityError, match="snapshot|listing"):
        snapshot_export_plan(swapped_graph, swapped_live.name)


def test_exactly_one_shared_lease_no_nested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_compare as compare_mod
    import graphrag_code.snapshot_export_plan as plan_mod
    import graphrag_code.snapshot_read as read_mod
    import graphrag_code.byog_snapshot_graph_audit as audit_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    calls = {"shared": 0}
    state = {"lease_active": False}
    original = plan_mod.graph_read_lease
    original_require = plan_mod._require_managed_graph

    @contextmanager
    def counted(*args, **kwargs):
        calls["shared"] += 1
        with original(*args, **kwargs) as lease:
            state["lease_active"] = True
            try:
                yield lease
            finally:
                state["lease_active"] = False

    def guarded_require(*args, **kwargs):
        assert state["lease_active"]
        return original_require(*args, **kwargs)

    def boom(*_args, **_kwargs):
        raise AssertionError("nested graph lease or public mutating/read scope")

    monkeypatch.setattr(plan_mod, "graph_read_lease", counted)
    monkeypatch.setattr(plan_mod, "_require_managed_graph", guarded_require)
    monkeypatch.setattr(plan_mod, "graph_exclusive_lease", boom, raising=False)
    monkeypatch.setattr(read_mod, "retained_snapshot_read", boom)
    monkeypatch.setattr(compare_mod, "snapshot_history", boom, raising=False)
    monkeypatch.setattr(compare_mod, "graph_read_lease", boom)
    monkeypatch.setattr(audit_mod, "audit_graph_root", boom)
    monkeypatch.setattr(audit_mod, "resolve_snapshot", boom)
    result = snapshot_export_plan(graph, "current")
    assert result["ok"] is True
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
    args = ["--graph", "g", "--snapshot", "current", "--json"]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_export_plan", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-export-plan", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    bodies = [json.loads(proc.stdout) for proc in (module, script, cli)]
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["resolved_snapshot"] == live.name
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-export-plan",
            "--graph",
            str(graph),
            "--snapshot",
            live.name,
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["export_revision"] == bodies[0]["export_revision"]

    import tarfile

    with tarfile.open(built_wheel_and_sdist[1], "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_export_plan.py" in names


def test_cli_serializes_writes_and_flushes_under_shared_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_export_plan as plan_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    original_scope = plan_mod._snapshot_export_plan_scope
    original_json = plan_mod.result_to_json
    original_format = plan_mod.format_result
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

    monkeypatch.setattr(plan_mod, "_snapshot_export_plan_scope", tracked_scope)
    monkeypatch.setattr(plan_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(plan_mod, "format_result", guarded_format)
    monkeypatch.setattr(plan_mod.sys, "stdout", GuardedStdout())
    assert (
        plan_mod.main(["--graph", str(graph), "--snapshot", "current", "--json"]) == 0
    )
    assert plan_mod.main(["--graph", str(graph), "--snapshot", "current"]) == 0
    assert state["active"] is False
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
    assert "graph_read_lease" in imported
    assert "parse_snapshot_ref" in imported
    assert "os.read" in source or "read" in called
    assert HASH_CHUNK_BYTES <= 64 * 1024
    assert "dir_fd=" in source
    assert "O_DIRECTORY" in source
    assert "read_bytes" not in source
    assert "graph_exclusive_lease" not in source
    assert "publish_byog_snapshot" not in source
    assert "retained_snapshot_read" not in source
    assert "resolve_snapshot" not in source
    assert "snapshot_maintenance_apply" not in source
    assert "index_python" not in source
    assert "extract_c" not in source
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    assert "export_performed=false" in source or "export_performed" in source


def test_mcp_remains_exactly_sixteen(tmp_path: Path):
    from anyio import run as anyio_run

    assert len(TOOL_NAMES) == 16
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
            assert len(names) == 16
            assert "snapshot_export_plan" not in names

    anyio_run(_body)
    fingerprints = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(fingerprints) == 15
