"""CAS-verified composite snapshot maintenance apply.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_maintenance_apply.py -q
"""
from __future__ import annotations

import ast
import hashlib
import json
import multiprocessing
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
    STAGING_NAME_PREFIX,
    STAGING_WRITER_LOCK_NAME,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_maintenance_apply import (  # type: ignore
    SnapshotMaintenanceApplyError,
    SnapshotMaintenanceApplyIntegrityError,
    format_result,
    parse_maintenance_revision,
    result_to_json,
    snapshot_maintenance_apply,
)
from graphrag_code.snapshot_maintenance_plan import (  # type: ignore
    snapshot_maintenance_plan,
)
from graphrag_code.snapshot_pins import (  # type: ignore
    ABSENT_REVISION,
    OPERATOR_PINS_NAME,
    snapshot_pin,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
SCRIPT = ROOT / "scripts" / "snapshot_maintenance_apply.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_maintenance_apply.py"
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
        "c_clang_ast_capture",
        "c_compiler_facts",
        "graph_read_lease",
        "staging_writer_lease",
        "_open_or_create_staging_writer_lock_fd",
        "snapshot_prune",
        "snapshot_staging_cleanup",
        "snapshot_maintenance_plan",
        "_snapshot_prune_scope",
        "_snapshot_staging_cleanup_scope",
        "_snapshot_maintenance_plan_scope",
    }
)
FORBIDDEN_WORDS = ("orphaned", "abandoned", "stale", "expired", "safe to delete")


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
        settings_text=f"maintenance-apply: {marker}\n",
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


def _apply_args(graph: Path, keep_last: int, revision: str, *flags: str) -> list[str]:
    return [
        "--graph",
        str(graph),
        "--keep-last",
        str(keep_last),
        "--expected-maintenance-revision",
        revision,
        *flags,
    ]


def _staging_dir(graph: Path, suffix: str) -> Path:
    path = graph / "snapshots" / f"{STAGING_NAME_PREFIX}{suffix}"
    path.mkdir()
    return path


def _write_complete_payload(directory: Path) -> None:
    (directory / "manifest.json").write_text("{}\n", encoding="utf-8")
    (directory / "entities.parquet").write_bytes(b"ENT")
    (directory / "relationships.parquet").write_bytes(b"REL")
    (directory / "text_units.parquet").write_bytes(b"TU")
    (directory / "call_observations.parquet").write_bytes(b"CO")
    (directory / "settings.yaml").write_text("k: v\n", encoding="utf-8")


def _write_writer_lock(directory: Path, payload: bytes = b"") -> Path:
    lock = directory / STAGING_WRITER_LOCK_NAME
    lock.write_bytes(payload)
    return lock


def _cooperative_leftover(graph: Path, suffix: str, *, complete: bool) -> Path:
    staging = _staging_dir(graph, suffix)
    if complete:
        _write_complete_payload(staging)
    else:
        (staging / "manifest.json").write_text("{}\n", encoding="utf-8")
    _write_writer_lock(staging)
    return staging


def _assert_success_shape(result: dict, plan: dict) -> None:
    assert result["schema_version"] == 1
    assert result["ok"] is True
    assert result["partial"] is False
    assert result["retry_requires_fresh_plan"] is False
    assert result["fresh_plan_required_after_any_apply"] is True
    assert result["keep_last"] == plan["keep_last"]
    assert result["current"] == plan["current"]
    assert result["expected_maintenance_revision"] == plan["maintenance_revision"]
    assert result["observed_maintenance_revision"] == plan["maintenance_revision"]
    assert (
        result["observed_retention_plan_revision"]
        == plan["retention_plan"]["plan_revision"]
    )
    assert (
        result["observed_staging_cleanup_plan_revision"]
        == plan["staging_cleanup_plan"]["plan_revision"]
    )
    assert result["component_apply_order"] == [
        "snapshot-staging-cleanup",
        "snapshot-prune",
    ]
    assert result["maintenance_confirmed"] is True
    assert result["stopped_on_component"] is None
    assert result["not_attempted_components"] == []
    assert "fresh_plan_required_after_any_apply=true" in format_result(result)
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == [
        "apply_order_is_command_execution",
        "plan_observation_is_not_claim",
        "advisory_locks_cooperating_only",
        "recursive_deletion_not_atomic",
        "fresh_plan_required_after_any_apply",
        "cli_only_not_mcp",
    ]


def test_empty_successful_apply(tmp_path: Path):
    graph = tmp_path / "g"
    only = _publish(graph, "only")
    plan = snapshot_maintenance_plan(graph, 1)
    assert plan["actionable_components"] == []
    before = _protected_state(graph)
    result = snapshot_maintenance_apply(
        graph, 1, plan["maintenance_revision"], maintenance_confirmed=True
    )
    _assert_success_shape(result, plan)
    assert result["changed"] is False
    assert result["filesystem_may_have_changed"] is False
    assert result["completed_components"] == []
    assert result["deleted_snapshots"] == []
    assert result["deleted_staging_entries"] == []
    assert only.is_dir()
    assert _protected_state(graph) == before


def test_prune_only_cleanup_only_and_both(tmp_path: Path):
    prune_graph = tmp_path / "prune"
    old = _publish(prune_graph, "old")
    pinned = _publish(prune_graph, "pinned")
    live = _publish(prune_graph, "live")
    snapshot_pin(
        prune_graph, pinned.name, ABSENT_REVISION, pin_confirmed=True
    )
    prune_plan = snapshot_maintenance_plan(prune_graph, 1)
    assert prune_plan["actionable_components"] == ["snapshot-prune"]
    assert old.name in prune_plan["retention_plan"]["deletion_candidates"]
    assert pinned.name not in prune_plan["retention_plan"]["deletion_candidates"]
    prune_result = snapshot_maintenance_apply(
        prune_graph, 1, prune_plan["maintenance_revision"], maintenance_confirmed=True
    )
    _assert_success_shape(prune_result, prune_plan)
    assert prune_result["changed"] is True
    assert prune_result["completed_components"] == ["snapshot-prune"]
    assert prune_result["deleted_snapshots"] == prune_plan["retention_plan"][
        "deletion_candidates"
    ]
    assert prune_result["deleted_staging_entries"] == []
    assert not old.exists()
    assert pinned.is_dir()
    assert live.is_dir()
    assert _current(prune_graph) == live.name

    cleanup_graph = tmp_path / "cleanup"
    published = _publish(cleanup_graph, "live")
    leftover = _cooperative_leftover(
        cleanup_graph, "20240101-000000-only", complete=True
    )
    cleanup_plan = snapshot_maintenance_plan(cleanup_graph, 1)
    assert cleanup_plan["actionable_components"] == ["snapshot-staging-cleanup"]
    cleanup_result = snapshot_maintenance_apply(
        cleanup_graph,
        1,
        cleanup_plan["maintenance_revision"],
        maintenance_confirmed=True,
    )
    _assert_success_shape(cleanup_result, cleanup_plan)
    assert cleanup_result["changed"] is True
    assert cleanup_result["completed_components"] == ["snapshot-staging-cleanup"]
    assert cleanup_result["deleted_staging_entries"] == [leftover.name]
    assert cleanup_result["deleted_snapshots"] == []
    assert not leftover.exists()
    assert published.is_dir()

    both_graph = tmp_path / "both"
    first = _publish(both_graph, "old")
    second = _publish(both_graph, "new")
    leftover_a = _cooperative_leftover(
        both_graph, "20240101-000000-aaa", complete=True
    )
    leftover_z = _cooperative_leftover(
        both_graph, "20240101-000000-zzz", complete=False
    )
    both_plan = snapshot_maintenance_plan(both_graph, 1)
    assert both_plan["actionable_components"] == [
        "snapshot-prune",
        "snapshot-staging-cleanup",
    ]
    both_result = snapshot_maintenance_apply(
        both_graph, 1, both_plan["maintenance_revision"], maintenance_confirmed=True
    )
    _assert_success_shape(both_result, both_plan)
    assert both_result["changed"] is True
    assert both_result["completed_components"] == [
        "snapshot-staging-cleanup",
        "snapshot-prune",
    ]
    assert both_result["deleted_staging_entries"] == [leftover_a.name, leftover_z.name]
    assert both_result["deleted_snapshots"] == [first.name]
    assert not leftover_a.exists() and not leftover_z.exists()
    assert not first.exists()
    assert second.is_dir()
    assert _current(both_graph) == second.name


def test_stale_revision_changes_nothing(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "old")
    _publish(graph, "new")
    leftover = _cooperative_leftover(graph, "20240101-000000-stale", complete=True)
    preview = snapshot_maintenance_plan(graph, 1)
    _publish(graph, "newer")
    before = _protected_state(graph)
    with pytest.raises(
        SnapshotMaintenanceApplyIntegrityError, match="does not match"
    ):
        snapshot_maintenance_apply(
            graph, 1, preview["maintenance_revision"], maintenance_confirmed=True
        )
    assert leftover.is_dir()
    assert _protected_state(graph) == before
    proc = _run(
        *_apply_args(
            graph, 1, preview["maintenance_revision"], "--maintenance-confirmed", "--json"
        )
    )
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert leftover.is_dir()


def test_missing_confirmation_and_malformed_inputs(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_maintenance_plan(graph, 1)
    before = _protected_state(graph)
    missing = _run(*_apply_args(graph, 1, plan["maintenance_revision"], "--json"))
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert "--maintenance-confirmed" in missing.stderr
    with pytest.raises(SnapshotMaintenanceApplyError, match="maintenance-confirmed"):
        snapshot_maintenance_apply(
            graph, 1, plan["maintenance_revision"], maintenance_confirmed=False
        )
    for token in (
        "SHA256:" + "a" * 64,
        "sha256:" + "A" * 64,
        "sha256:" + "a" * 63,
        " sha256:" + "a" * 64,
        "not-a-revision",
    ):
        with pytest.raises(
            SnapshotMaintenanceApplyError,
            match="sha256:<64 lowercase hex>|whitespace",
        ):
            parse_maintenance_revision(token)
        bad = _run(*_apply_args(graph, 1, token, "--maintenance-confirmed", "--json"))
        assert bad.returncode == 2
        assert bad.stdout == ""
    zero = _run(
        *_apply_args(graph, 0, plan["maintenance_revision"], "--maintenance-confirmed")
    )
    assert zero.returncode == 2
    assert zero.stdout == ""
    assert "positive integer" in zero.stderr
    assert _protected_state(graph) == before


def test_exactly_one_exclusive_lease_no_nested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_apply as apply_mod
    import graphrag_code.snapshot_maintenance_plan as plan_mod
    import graphrag_code.snapshot_prune as prune_mod
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_maintenance_plan(graph, 1)
    calls = {"exclusive": 0}
    original = apply_mod.graph_exclusive_lease

    @contextmanager
    def counted(*args, **kwargs):
        calls["exclusive"] += 1
        with original(*args, **kwargs) as lease:
            yield lease

    def boom(*_args, **_kwargs):
        raise AssertionError("nested graph lease or public apply/plan scope")

    monkeypatch.setattr(apply_mod, "graph_exclusive_lease", counted)
    monkeypatch.setattr(apply_mod, "graph_read_lease", boom, raising=False)
    monkeypatch.setattr(plan_mod, "graph_read_lease", boom)
    monkeypatch.setattr(plan_mod, "snapshot_maintenance_plan", boom)
    monkeypatch.setattr(plan_mod, "_snapshot_maintenance_plan_scope", boom)
    monkeypatch.setattr(prune_mod, "snapshot_prune", boom)
    monkeypatch.setattr(prune_mod, "_snapshot_prune_scope", boom)
    monkeypatch.setattr(prune_mod, "graph_exclusive_lease", boom)
    monkeypatch.setattr(cleanup_mod, "snapshot_staging_cleanup", boom)
    monkeypatch.setattr(cleanup_mod, "_snapshot_staging_cleanup_scope", boom)
    monkeypatch.setattr(cleanup_mod, "graph_exclusive_lease", boom)
    result = snapshot_maintenance_apply(
        graph, 1, plan["maintenance_revision"], maintenance_confirmed=True
    )
    assert result["ok"] is True
    assert calls["exclusive"] == 1


def test_all_writer_locks_claimed_before_first_unlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_apply as apply_mod
    import graphrag_code.snapshot_prune as prune_mod
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    first = _publish(graph, "old")
    _publish(graph, "new")
    leftover_a = _cooperative_leftover(graph, "20240101-000000-aaa", complete=True)
    leftover_z = _cooperative_leftover(graph, "20240101-000000-zzz", complete=True)
    plan = snapshot_maintenance_plan(graph, 1)
    phases: list[str] = []
    orig_claims = apply_mod._after_maintenance_writer_claims
    orig_revalidate = apply_mod._after_maintenance_revalidation
    orig_before = apply_mod._before_maintenance_deletion
    orig_staging = cleanup_mod._remove_claimed_staging_entry
    orig_prune = prune_mod._remove_published_snapshot_directory

    def after_claims(claims):
        phases.append(f"claims:{len(claims)}")
        assert leftover_a.is_dir() and leftover_z.is_dir() and first.is_dir()
        return orig_claims(claims)

    def after_revalidate(root, claims):
        phases.append(f"revalidate:{len(claims)}")
        assert leftover_a.is_dir() and leftover_z.is_dir() and first.is_dir()
        return orig_revalidate(root, claims)

    def before_delete(root):
        phases.append("before_delete")
        assert leftover_a.is_dir() and leftover_z.is_dir() and first.is_dir()
        return orig_before(root)

    def track_staging(snapshots_dir, name, claim, expected):
        phases.append(f"staging:{name}")
        return orig_staging(snapshots_dir, name, claim, expected)

    def track_prune(snapshots_dir, snap_id):
        phases.append(f"prune:{snap_id}")
        return orig_prune(snapshots_dir, snap_id)

    monkeypatch.setattr(apply_mod, "_after_maintenance_writer_claims", after_claims)
    monkeypatch.setattr(apply_mod, "_after_maintenance_revalidation", after_revalidate)
    monkeypatch.setattr(apply_mod, "_before_maintenance_deletion", before_delete)
    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", track_staging)
    monkeypatch.setattr(prune_mod, "_remove_published_snapshot_directory", track_prune)
    result = snapshot_maintenance_apply(
        graph, 1, plan["maintenance_revision"], maintenance_confirmed=True
    )
    assert phases[:3] == ["claims:2", "revalidate:2", "before_delete"]
    assert phases[3:5] == [f"staging:{leftover_a.name}", f"staging:{leftover_z.name}"]
    assert phases[5:] == [f"prune:{first.name}"]
    assert result["deleted_staging_entries"] == [leftover_a.name, leftover_z.name]
    assert result["deleted_snapshots"] == [first.name]


def test_contention_unsafe_replaced_writer_locks_fail_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_apply as apply_mod

    graph = tmp_path / "g"
    published = _publish(graph, "live")
    leftover = _cooperative_leftover(graph, "20240101-000000-held", complete=True)
    plan = snapshot_maintenance_plan(graph, 1)
    held = CTX.Event()
    resume = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(
        target=_ungated_writer_lease_hold, args=(str(leftover), held, resume, q)
    )
    original = apply_mod._after_maintenance_plan_recompute

    def acquire_after_recompute(root, recomputed, consistency):
        original(root, recomputed, consistency)
        holder.start()
        assert held.wait(timeout=TIMEOUT)

    monkeypatch.setattr(
        apply_mod, "_after_maintenance_plan_recompute", acquire_after_recompute
    )
    before = _protected_state(graph)
    try:
        with pytest.raises(
            SnapshotMaintenanceApplyIntegrityError, match="held by a cooperating"
        ):
            snapshot_maintenance_apply(
                graph, 1, plan["maintenance_revision"], maintenance_confirmed=True
            )
        assert leftover.is_dir()
        assert published.is_dir()
        assert _protected_state(graph) == before
    finally:
        monkeypatch.setattr(
            apply_mod, "_after_maintenance_plan_recompute", original
        )
        _cleanup_processes(holder, release=resume)

    graph2 = tmp_path / "g2"
    _publish(graph2, "live")
    leftover2 = _cooperative_leftover(graph2, "20240101-000000-repl", complete=True)
    plan2 = snapshot_maintenance_plan(graph2, 1)
    outside = tmp_path / "must-not-follow"
    outside.write_bytes(b"keep-me")

    def replace_after_claims(claims):
        lock = leftover2 / STAGING_WRITER_LOCK_NAME
        parked = tmp_path / "parked.writer.lock"
        lock.rename(parked)
        lock.symlink_to(outside)

    monkeypatch.setattr(apply_mod, "_after_maintenance_writer_claims", replace_after_claims)
    with pytest.raises(SnapshotMaintenanceApplyIntegrityError):
        snapshot_maintenance_apply(
            graph2, 1, plan2["maintenance_revision"], maintenance_confirmed=True
        )
    assert leftover2.is_dir()
    assert outside.read_bytes() == b"keep-me"


def test_current_pins_published_staging_and_lock_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_apply as apply_mod

    graph = tmp_path / "g"
    first = _publish(graph, "old")
    second = _publish(graph, "new")
    leftover = _cooperative_leftover(graph, "20240101-000000-race", complete=True)
    preview = snapshot_maintenance_plan(graph, 1)

    def switch_current(root, plan, consistency):
        (root / "current").write_text(first.name + "\n", encoding="utf-8")

    monkeypatch.setattr(apply_mod, "_after_maintenance_plan_recompute", switch_current)
    with pytest.raises(SnapshotMaintenanceApplyIntegrityError):
        snapshot_maintenance_apply(
            graph, 1, preview["maintenance_revision"], maintenance_confirmed=True
        )
    assert leftover.is_dir() and first.is_dir() and second.is_dir()
    (graph / "current").write_text(second.name + "\n", encoding="utf-8")

    def add_pin(root, plan, consistency):
        (root / OPERATOR_PINS_NAME).write_text(
            '{"schema_version": 1, "pins": []}\n', encoding="utf-8"
        )

    preview2 = snapshot_maintenance_plan(graph, 1)
    monkeypatch.setattr(apply_mod, "_after_maintenance_plan_recompute", add_pin)
    with pytest.raises(SnapshotMaintenanceApplyIntegrityError):
        snapshot_maintenance_apply(
            graph, 1, preview2["maintenance_revision"], maintenance_confirmed=True
        )
    assert leftover.is_dir()

    graph2 = tmp_path / "g2"
    _publish(graph2, "a")
    extra = _publish(graph2, "b")
    leftover2 = _cooperative_leftover(graph2, "20240101-000000-pub", complete=True)
    preview3 = snapshot_maintenance_plan(graph2, 1)

    def add_published(root, plan, consistency):
        extra.rename(tmp_path / "parked-b")

    monkeypatch.setattr(apply_mod, "_after_maintenance_plan_recompute", add_published)
    with pytest.raises(SnapshotMaintenanceApplyIntegrityError):
        snapshot_maintenance_apply(
            graph2, 1, preview3["maintenance_revision"], maintenance_confirmed=True
        )
    assert leftover2.is_dir()

    graph3 = tmp_path / "g3"
    _publish(graph3, "live")
    leftover3 = _cooperative_leftover(graph3, "20240101-000000-struct", complete=True)
    preview4 = snapshot_maintenance_plan(graph3, 1)

    def change_structure(root, plan, consistency):
        (leftover3 / "entities.parquet").write_bytes(b"CHANGED")

    monkeypatch.setattr(apply_mod, "_after_maintenance_plan_recompute", change_structure)
    with pytest.raises(
        SnapshotMaintenanceApplyIntegrityError,
        match="structure changed|does not match",
    ):
        snapshot_maintenance_apply(
            graph3, 1, preview4["maintenance_revision"], maintenance_confirmed=True
        )
    assert leftover3.is_dir()

    graph4 = tmp_path / "g4"
    _publish(graph4, "live")
    leftover4 = _cooperative_leftover(graph4, "20240101-000000-glock", complete=True)
    preview5 = snapshot_maintenance_plan(graph4, 1)
    lock = graph4 / PUBLICATION_LOCK_NAME
    payload = lock.read_bytes()

    def replace_lock(root, plan, consistency):
        lock.unlink()
        lock.write_bytes(payload)

    monkeypatch.setattr(apply_mod, "_after_maintenance_plan_recompute", replace_lock)
    with pytest.raises(
        SnapshotMaintenanceApplyIntegrityError,
        match="publication_lock|does not match",
    ):
        snapshot_maintenance_apply(
            graph4, 1, preview5["maintenance_revision"], maintenance_confirmed=True
        )
    assert leftover4.is_dir()


def test_post_plan_inputs_do_not_become_a_new_apply_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_apply as apply_mod
    import graphrag_code.snapshot_retention as retention_mod

    graph = tmp_path / "published"
    old = _publish(graph, "old")
    live = _publish(graph, "live")
    preview = snapshot_maintenance_plan(graph, 1)
    original_build = apply_mod.build_stable_maintenance_plan_unlocked

    def add_published_after_plan(root: Path, keep_last: int):
        consistency, plan = original_build(root, keep_last)
        shutil.copytree(
            live,
            root / "snapshots" / "20990101-000000-deadbeef",
        )
        return consistency, plan

    monkeypatch.setattr(
        apply_mod,
        "build_stable_maintenance_plan_unlocked",
        add_published_after_plan,
    )
    with pytest.raises(
        SnapshotMaintenanceApplyIntegrityError,
        match="published_snapshots",
    ):
        snapshot_maintenance_apply(
            graph,
            1,
            preview["maintenance_revision"],
            maintenance_confirmed=True,
        )
    assert old.is_dir() and live.is_dir()

    claim_graph = tmp_path / "claims"
    claim_old = _publish(claim_graph, "old")
    _publish(claim_graph, "live")
    claim_preview = snapshot_maintenance_plan(claim_graph, 1)
    monkeypatch.setattr(
        apply_mod,
        "build_stable_maintenance_plan_unlocked",
        original_build,
    )
    original_claim_pins = retention_mod._claim_pins
    calls = {"count": 0}

    def claim_after_plan(root: Path):
        calls["count"] += 1
        if calls["count"] <= 2:
            return original_claim_pins(root)
        return [claim_old.name]

    monkeypatch.setattr(retention_mod, "_claim_pins", claim_after_plan)
    monkeypatch.setattr(
        apply_mod,
        "_claim_pins",
        lambda _root: [claim_old.name],
        raising=False,
    )
    with pytest.raises(
        SnapshotMaintenanceApplyIntegrityError,
        match="claim_pins",
    ):
        snapshot_maintenance_apply(
            claim_graph,
            1,
            claim_preview["maintenance_revision"],
            maintenance_confirmed=True,
        )
    assert claim_old.is_dir()


def test_pin_change_after_staging_cleanup_stops_before_prune(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_apply as apply_mod

    graph = tmp_path / "g"
    old = _publish(graph, "old")
    live = _publish(graph, "live")
    leftover = _cooperative_leftover(
        graph, "20240101-000000-pin-before-prune", complete=True
    )
    preview = snapshot_maintenance_plan(graph, 1)

    def pin_before_first_unlink(root: Path) -> None:
        (root / OPERATOR_PINS_NAME).write_text(
            json.dumps({"schema_version": 1, "pins": [old.name]}) + "\n",
            encoding="utf-8",
        )

    monkeypatch.setattr(
        apply_mod, "_before_maintenance_deletion", pin_before_first_unlink
    )
    result = snapshot_maintenance_apply(
        graph,
        1,
        preview["maintenance_revision"],
        maintenance_confirmed=True,
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["fresh_plan_required_after_any_apply"] is True
    assert result["completed_components"] == ["snapshot-staging-cleanup"]
    assert result["stopped_on_component"] == "snapshot-prune"
    assert result["deleted_staging_entries"] == [leftover.name]
    assert result["deleted_snapshots"] == []
    assert result["not_attempted_snapshots"] == [old.name]
    assert not leftover.exists()
    assert old.is_dir() and live.is_dir()


def test_deterministic_cleanup_then_prune_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_prune as prune_mod
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    first = _publish(graph, "old")
    _publish(graph, "new")
    leftover = _cooperative_leftover(graph, "20240101-000000-order", complete=True)
    plan = snapshot_maintenance_plan(graph, 1)
    assert plan["actionable_components"][0] == "snapshot-prune"
    seen: list[str] = []
    orig_staging = cleanup_mod._remove_claimed_staging_entry
    orig_prune = prune_mod._remove_published_snapshot_directory

    def track_staging(snapshots_dir, name, claim, expected):
        seen.append(f"cleanup:{name}")
        return orig_staging(snapshots_dir, name, claim, expected)

    def track_prune(snapshots_dir, snap_id):
        seen.append(f"prune:{snap_id}")
        return orig_prune(snapshots_dir, snap_id)

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", track_staging)
    monkeypatch.setattr(prune_mod, "_remove_published_snapshot_directory", track_prune)
    result = snapshot_maintenance_apply(
        graph, 1, plan["maintenance_revision"], maintenance_confirmed=True
    )
    assert seen == [f"cleanup:{leftover.name}", f"prune:{first.name}"]
    assert result["completed_components"] == [
        "snapshot-staging-cleanup",
        "snapshot-prune",
    ]


def test_partial_failure_in_each_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_prune as prune_mod
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "cleanup-partial"
    published = _publish(graph, "old")
    _publish(graph, "new")
    first = _cooperative_leftover(graph, "20240101-000000-aaa", complete=True)
    second = _cooperative_leftover(graph, "20240101-000000-zzz", complete=True)
    plan = snapshot_maintenance_plan(graph, 1)
    orig_staging = cleanup_mod._remove_claimed_staging_entry
    calls = {"n": 0}

    def fail_later_staging(snapshots_dir, name, claim, expected):
        calls["n"] += 1
        if calls["n"] == 1:
            return orig_staging(snapshots_dir, name, claim, expected)
        raise RuntimeError(f"injected cleanup failure on {name}")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", fail_later_staging)
    result = snapshot_maintenance_apply(
        graph, 1, plan["maintenance_revision"], maintenance_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["changed"] is True
    assert result["filesystem_may_have_changed"] is True
    assert result["retry_requires_fresh_plan"] is True
    assert result["completed_components"] == []
    assert result["stopped_on_component"] == "snapshot-staging-cleanup"
    assert result["not_attempted_components"] == ["snapshot-prune"]
    assert result["deleted_staging_entries"] == [first.name]
    assert result["failed_staging_entry"] == second.name
    assert result["not_attempted_staging_entries"] == []
    assert result["deleted_snapshots"] == []
    assert result["not_attempted_snapshots"] == plan["retention_plan"][
        "deletion_candidates"
    ]
    assert not first.exists()
    assert second.is_dir()
    assert published.is_dir()
    text = format_result(result)
    assert "PARTIAL FAILURE" in text
    assert "no rollback" in text.lower()

    graph2 = tmp_path / "prune-partial"
    old = _publish(graph2, "old")
    mid = _publish(graph2, "mid")
    live = _publish(graph2, "live")
    leftover = _cooperative_leftover(graph2, "20240101-000000-ok", complete=True)
    plan2 = snapshot_maintenance_plan(graph2, 1)
    orig_prune = prune_mod._remove_published_snapshot_directory
    prune_calls = {"n": 0}

    def fail_later_prune(snapshots_dir, snap_id):
        prune_calls["n"] += 1
        if prune_calls["n"] == 1:
            return orig_prune(snapshots_dir, snap_id)
        raise RuntimeError(f"injected prune failure on {snap_id}")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", orig_staging)
    monkeypatch.setattr(prune_mod, "_remove_published_snapshot_directory", fail_later_prune)
    result2 = snapshot_maintenance_apply(
        graph2, 1, plan2["maintenance_revision"], maintenance_confirmed=True
    )
    candidates = plan2["retention_plan"]["deletion_candidates"]
    assert result2["ok"] is False
    assert result2["partial"] is True
    assert result2["changed"] is True
    assert result2["completed_components"] == ["snapshot-staging-cleanup"]
    assert result2["stopped_on_component"] == "snapshot-prune"
    assert result2["not_attempted_components"] == []
    assert result2["deleted_staging_entries"] == [leftover.name]
    assert result2["deleted_snapshots"] == [candidates[0]]
    assert result2["failed_snapshot"] == candidates[1]
    assert result2["not_attempted_snapshots"] == candidates[2:]
    assert not leftover.exists()
    assert not (graph2 / "snapshots" / candidates[0]).exists()
    assert (graph2 / "snapshots" / candidates[1]).is_dir()
    assert live.is_dir()
    assert old.name in {candidates[0], candidates[1], live.name}
    assert mid.name in {candidates[0], candidates[1], live.name}


def test_relative_cwd_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    graph = here / "g"
    _publish(graph, "only")
    plan = snapshot_maintenance_plan(graph, 1)
    args = [
        "--graph",
        "g",
        "--keep-last",
        "1",
        "--expected-maintenance-revision",
        plan["maintenance_revision"],
        "--maintenance-confirmed",
        "--json",
    ]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_maintenance_apply", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-maintenance-apply", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    bodies = [json.loads(proc.stdout) for proc in (module, script, cli)]
    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0]["changed"] is False
    assert result_to_json(bodies[0]) == module.stdout

    leftover = _cooperative_leftover(graph, "20240101-000000-wheel", complete=True)
    plan2 = snapshot_maintenance_plan(graph, 1)
    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-maintenance-apply",
            "--graph",
            str(graph),
            "--keep-last",
            "1",
            "--expected-maintenance-revision",
            plan2["maintenance_revision"],
            "--maintenance-confirmed",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["deleted_staging_entries"] == [leftover.name]
    assert not leftover.exists()

    sdist = built_wheel_and_sdist[1]
    import tarfile

    with tarfile.open(sdist, "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_maintenance_apply.py" in names


def test_cli_serializes_writes_and_flushes_under_exclusive_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_maintenance_apply as apply_mod
    import graphrag_code.snapshot_staging_cleanup as cleanup_mod

    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_maintenance_plan(graph, 1)
    original_scope = apply_mod._snapshot_maintenance_apply_scope
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

    monkeypatch.setattr(apply_mod, "_snapshot_maintenance_apply_scope", tracked_scope)
    monkeypatch.setattr(apply_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(apply_mod, "format_result", guarded_format)
    monkeypatch.setattr(apply_mod.sys, "stdout", GuardedStdout())
    assert (
        apply_mod.main(
            _apply_args(
                graph, 1, plan["maintenance_revision"], "--maintenance-confirmed", "--json"
            )
        )
        == 0
    )

    leftover = _cooperative_leftover(graph, "20240101-000000-partial", complete=True)
    leftover2 = _cooperative_leftover(graph, "20240101-000000-later", complete=True)
    plan2 = snapshot_maintenance_plan(graph, 1)
    orig = cleanup_mod._remove_claimed_staging_entry
    calls = {"n": 0}

    def fail_later(snapshots_dir, name, claim, expected):
        calls["n"] += 1
        if calls["n"] == 1:
            return orig(snapshots_dir, name, claim, expected)
        raise RuntimeError("injected later")

    monkeypatch.setattr(cleanup_mod, "_remove_claimed_staging_entry", fail_later)
    assert (
        apply_mod.main(
            _apply_args(graph, 1, plan2["maintenance_revision"], "--maintenance-confirmed")
        )
        == 1
    )
    assert state["active"] is False
    assert state["responses"] >= 2
    assert state["flushes"] == 2
    assert leftover2.exists() is False
    assert leftover.exists() is True


def test_implementation_does_not_invoke_producers_or_public_scopes():
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
    assert called & FORBIDDEN == set()
    assert imported & {
        "index_python",
        "index_c",
        "extract_c",
        "extract_python",
        "graph_read_lease",
    } == set()
    assert "graph_exclusive_lease" in imported
    assert "build_stable_maintenance_plan_unlocked" in imported
    assert "snapshot_prune" not in called
    assert "snapshot_staging_cleanup" not in called
    assert "snapshot_maintenance_plan" not in called
    assert "_snapshot_prune_scope" not in called
    assert "_snapshot_staging_cleanup_scope" not in called
    assert "_snapshot_maintenance_plan_scope" not in called
    assert "publish_byog_snapshot" not in source
    assert "cleanup_old_snapshots" not in source
    assert "index_python" not in source
    assert "extract_c" not in source
    assert "graph_read_lease" not in source
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered


def test_mcp_remains_exactly_eleven(tmp_path: Path):
    from anyio import run as anyio_run

    assert len(TOOL_NAMES) == 12
    assert "snapshot_maintenance_apply" not in TOOL_NAMES
    assert "snapshot_maintenance_plan" not in TOOL_NAMES
    graph = tmp_path / "g"
    _publish(graph, "a")
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 12
            assert "snapshot_maintenance_apply" not in names

    anyio_run(_body)
    fingerprints = {path.name: _root_fingerprint(path) for path in BYOG_ROOTS}
    assert len(fingerprints) == 15


def _ungated_writer_lease_hold(stage_dir: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import graphrag_code.byog_graph as byog

    @contextmanager
    def no_graph_gate(_stage_dir):
        yield

    byog._staging_writer_acquisition_gate = no_graph_gate
    try:
        with byog.staging_writer_lease(ChildPath(stage_dir)):
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put("held")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")
