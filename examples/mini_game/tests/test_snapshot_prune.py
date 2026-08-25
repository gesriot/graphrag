"""Operator-triggered snapshot prune guarded by plan_revision.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_prune.py -q
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
from graphrag_code.snapshot_activate import snapshot_activate  # type: ignore
from graphrag_code.snapshot_pins import (  # type: ignore
    ABSENT_REVISION,
    OPERATOR_PINS_NAME,
    canonical_registry_text,
    snapshot_pin,
)
from graphrag_code.snapshot_prune import (  # type: ignore
    SnapshotPruneError,
    SnapshotPruneIntegrityError,
    format_result,
    parse_plan_revision,
    result_to_json,
    snapshot_prune,
)
from graphrag_code.snapshot_retention import snapshot_retention_plan  # type: ignore

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
SCRIPT = ROOT / "scripts" / "snapshot_prune.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_prune.py"
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
    }
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
        settings_text=f"snapshot-prune: {marker}\n",
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
        "current": _current(graph) if (graph / "current").is_file() else None,
        "listing": tuple(
            sorted(path.name for path in (graph / "snapshots").iterdir())
        )
        if (graph / "snapshots").is_dir()
        else (),
        "registry_exists": registry.exists(),
        "registry": registry.read_bytes() if registry.is_file() else None,
        "lock": (graph / PUBLICATION_LOCK_NAME).read_bytes()
        if (graph / PUBLICATION_LOCK_NAME).is_file()
        else None,
        "lock_stat": (
            (graph / PUBLICATION_LOCK_NAME).lstat().st_ino,
            (graph / PUBLICATION_LOCK_NAME).lstat().st_dev,
            (graph / PUBLICATION_LOCK_NAME).lstat().st_size,
            (graph / PUBLICATION_LOCK_NAME).lstat().st_mtime_ns,
        )
        if (graph / PUBLICATION_LOCK_NAME).exists()
        else None,
    }


def _retained_state(graph: Path, retained: list[str]) -> dict[str, object]:
    state = _protected_state(graph)
    keep = set(retained)
    state["hashes"] = {
        key: value
        for key, value in state["hashes"].items()
        if not key.startswith("snapshots/")
        or key.split("/", 2)[1] in keep
        or key == "snapshots"
    }
    state["stats"] = {
        key: value
        for key, value in state["stats"].items()
        if not key.startswith("snapshots/")
        or key.split("/", 2)[1] in keep
        or key == "snapshots"
    }
    return state


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )


def _prune_args(graph: Path, keep_last: int, revision: str, *extra: str) -> list[str]:
    return [
        "--graph",
        str(graph),
        "--keep-last",
        str(keep_last),
        "--expected-plan-revision",
        revision,
        *extra,
    ]


def test_confirmation_and_expected_revision_are_mandatory(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "older")
    _publish(graph, "newer")
    plan = snapshot_retention_plan(graph, 1)
    before = _protected_state(graph)
    with pytest.raises(SnapshotPruneError, match="--prune-confirmed"):
        snapshot_prune(
            graph, 1, plan["plan_revision"], prune_confirmed=False
        )
    missing_confirm = _run(
        *_prune_args(graph, 1, plan["plan_revision"], "--json")
    )
    assert missing_confirm.returncode == 2
    assert missing_confirm.stdout == ""
    assert "--prune-confirmed" in missing_confirm.stderr
    missing_rev = _run(
        "--graph",
        str(graph),
        "--keep-last",
        "1",
        "--prune-confirmed",
        "--json",
    )
    assert missing_rev.returncode == 2
    assert missing_rev.stdout == ""
    assert "expected-plan-revision" in missing_rev.stderr
    assert _protected_state(graph) == before


def test_malformed_revision_tokens(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    plan = snapshot_retention_plan(graph, 1)
    before = _protected_state(graph)
    tokens = [
        " " + plan["plan_revision"],
        plan["plan_revision"] + " ",
        plan["plan_revision"].upper(),
        plan["plan_revision"].replace("sha256:", "SHA256:"),
        plan["plan_revision"][:-1],
        plan["plan_revision"][7:],
        "absent",
        "md5:" + "a" * 32,
        "",
    ]
    for token in tokens:
        with pytest.raises(SnapshotPruneError, match="sha256"):
            parse_plan_revision(token)
        proc = _run(*_prune_args(graph, 1, token, "--prune-confirmed", "--json"))
        assert proc.returncode == 2, token
        assert proc.stdout == ""
        assert _protected_state(graph) == before


def test_successful_exact_prune(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    third = _publish(graph, "c")
    plan = snapshot_retention_plan(graph, 1)
    assert first.name in plan["deletion_candidates"]
    assert second.name in plan["deletion_candidates"]
    assert third.name in plan["retained_snapshots"]
    result = snapshot_prune(
        graph, 1, plan["plan_revision"], prune_confirmed=True
    )
    assert result["ok"] is True
    assert result["partial"] is False
    assert result["changed"] is True
    assert result["filesystem_may_have_changed"] is True
    assert result["failed_snapshot"] is None
    assert result["not_attempted_snapshots"] == []
    assert result["retry_requires_fresh_plan"] is False
    assert result["deleted_snapshots"] == plan["deletion_candidates"]
    assert result["deleted_count"] == 2
    assert result["remaining_published_snapshots"] == plan["retained_snapshots"]
    assert result["expected_plan_revision"] == plan["plan_revision"]
    assert result["observed_plan_revision"] == plan["plan_revision"]
    assert not first.exists()
    assert not second.exists()
    assert third.is_dir()
    assert _current(graph) == third.name


def test_no_candidate_idempotent_success(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "only")
    plan = snapshot_retention_plan(graph, 1)
    assert plan["deletion_candidates"] == []
    before = _protected_state(graph)
    result = snapshot_prune(
        graph, 1, plan["plan_revision"], prune_confirmed=True
    )
    assert result["ok"] is True
    assert result["changed"] is False
    assert result["filesystem_may_have_changed"] is False
    assert result["partial"] is False
    assert result["deleted_count"] == 0
    assert result["deleted_snapshots"] == []
    assert _protected_state(graph) == before


def test_stale_token_changes_nothing(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    plan = snapshot_retention_plan(graph, 1)
    stale = "sha256:" + "0" * 64
    before = _protected_state(graph)
    with pytest.raises(SnapshotPruneIntegrityError, match="does not match"):
        snapshot_prune(graph, 1, stale, prune_confirmed=True)
    proc = _run(*_prune_args(graph, 1, stale, "--prune-confirmed", "--json"))
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert _protected_state(graph) == before


def test_keep_last_mismatch_causes_stale_cas(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    _publish(graph, "c")
    preview = snapshot_retention_plan(graph, 2)
    before = _protected_state(graph)
    with pytest.raises(SnapshotPruneIntegrityError, match="does not match"):
        snapshot_prune(graph, 1, preview["plan_revision"], prune_confirmed=True)
    assert _protected_state(graph) == before


def test_changed_current_causes_stale_cas(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    preview = snapshot_retention_plan(graph, 1)
    snapshot_activate(
        graph, older.name, newer.name, activate_confirmed=True
    )
    before = _protected_state(graph)
    with pytest.raises(SnapshotPruneIntegrityError, match="does not match"):
        snapshot_prune(
            graph, 1, preview["plan_revision"], prune_confirmed=True
        )
    assert _protected_state(graph) == before


def test_changed_operator_registry_causes_stale_cas(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    _publish(graph, "newer")
    preview = snapshot_retention_plan(graph, 1)
    snapshot_pin(graph, older.name, ABSENT_REVISION, pin_confirmed=True)
    before = _protected_state(graph)
    with pytest.raises(SnapshotPruneIntegrityError, match="does not match"):
        snapshot_prune(
            graph, 1, preview["plan_revision"], prune_confirmed=True
        )
    assert _protected_state(graph) == before


def test_changed_claim_pins_causes_stale_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.byog_graph as byog_graph

    graph = tmp_path / "g"
    older = _publish(graph, "older")
    _publish(graph, "newer")
    preview = snapshot_retention_plan(graph, 1)
    monkeypatch.setattr(
        byog_graph, "pinned_snapshot_ids", lambda _root: {older.name}
    )
    before = _protected_state(graph)
    with pytest.raises(SnapshotPruneIntegrityError, match="does not match"):
        snapshot_prune(
            graph, 1, preview["plan_revision"], prune_confirmed=True
        )
    assert _protected_state(graph) == before


def test_added_or_removed_published_snapshot_causes_stale_cas(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a")
    _publish(graph, "b")
    preview = snapshot_retention_plan(graph, 1)
    _publish(graph, "c")
    before = _protected_state(graph)
    with pytest.raises(SnapshotPruneIntegrityError, match="does not match"):
        snapshot_prune(
            graph, 1, preview["plan_revision"], prune_confirmed=True
        )
    assert _protected_state(graph) == before

    graph2 = tmp_path / "g2"
    extra = _publish(graph2, "old")
    _publish(graph2, "new")
    preview2 = snapshot_retention_plan(graph2, 2)
    shutil.rmtree(extra)
    before2 = _protected_state(graph2)
    with pytest.raises(SnapshotPruneIntegrityError, match="does not match"):
        snapshot_prune(
            graph2, 2, preview2["plan_revision"], prune_confirmed=True
        )
    assert _protected_state(graph2) == before2


def test_current_pins_and_staging_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.byog_graph as byog_graph

    graph = tmp_path / "g"
    operator = _publish(graph, "operator")
    claimed = _publish(graph, "claimed")
    extra = _publish(graph, "extra")
    current = _publish(graph, "current")
    staging = graph / "snapshots" / f"{STAGING_NAME_PREFIX}{extra.name}"
    staging.mkdir()
    (staging / "marker.txt").write_text("staging", encoding="utf-8")
    snapshot_pin(graph, operator.name, ABSENT_REVISION, pin_confirmed=True)
    monkeypatch.setattr(
        byog_graph, "pinned_snapshot_ids", lambda _root: {claimed.name}
    )
    plan = snapshot_retention_plan(graph, 1)
    assert extra.name in plan["deletion_candidates"]
    assert operator.name in plan["retained_snapshots"]
    assert claimed.name in plan["retained_snapshots"]
    assert current.name in plan["retained_snapshots"]
    result = snapshot_prune(
        graph, 1, plan["plan_revision"], prune_confirmed=True
    )
    assert result["ok"] is True
    assert extra.exists() is False
    assert operator.is_dir()
    assert claimed.is_dir()
    assert current.is_dir()
    assert staging.is_dir()
    assert (staging / "marker.txt").read_text(encoding="utf-8") == "staging"
    assert _current(graph) == current.name
    assert (graph / OPERATOR_PINS_NAME).is_file()


def test_dangling_pins_are_not_deleted_as_paths(tmp_path: Path, monkeypatch):
    import graphrag_code.byog_graph as byog_graph

    graph = tmp_path / "g"
    _publish(graph, "a")
    newer = _publish(graph, "b")
    missing_op = "19990101-000000-deadbeef"
    missing_claim = "19990101-000000-cafebabe"
    (graph / OPERATOR_PINS_NAME).write_text(
        canonical_registry_text([missing_op]), encoding="utf-8"
    )
    monkeypatch.setattr(
        byog_graph, "pinned_snapshot_ids", lambda _root: {missing_claim}
    )
    plan = snapshot_retention_plan(graph, 1)
    assert missing_op in plan["dangling_operator_pins"]
    assert missing_claim in plan["dangling_claim_pins"]
    assert missing_op not in plan["deletion_candidates"]
    assert missing_claim not in plan["deletion_candidates"]
    result = snapshot_prune(
        graph, 1, plan["plan_revision"], prune_confirmed=True
    )
    assert result["ok"] is True
    assert not (graph / "snapshots" / missing_op).exists()
    assert not (graph / "snapshots" / missing_claim).exists()
    assert newer.is_dir()
    assert missing_op not in result["deleted_snapshots"]
    assert missing_claim not in result["deleted_snapshots"]


def test_unsafe_candidate_state_fails_before_deletion(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    plan = snapshot_retention_plan(graph, 1)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep-me.txt"
    marker.write_text("safe", encoding="utf-8")
    shutil.rmtree(first)
    first.symlink_to(outside, target_is_directory=True)
    before_listing = tuple(
        sorted(path.name for path in (graph / "snapshots").iterdir())
    )
    proc = _run(
        *_prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
    )
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert first.is_symlink()
    assert second.is_dir()
    assert marker.read_text(encoding="utf-8") == "safe"
    assert tuple(
        sorted(path.name for path in (graph / "snapshots").iterdir())
    ) == before_listing

    graph2 = tmp_path / "g2"
    file_cand = _publish(graph2, "x")
    keep = _publish(graph2, "y")
    plan2 = snapshot_retention_plan(graph2, 1)
    shutil.rmtree(file_cand)
    file_cand.write_text("not a directory", encoding="utf-8")
    proc2 = _run(
        *_prune_args(graph2, 1, plan2["plan_revision"], "--prune-confirmed", "--json")
    )
    assert proc2.returncode != 0
    assert proc2.stdout == ""
    assert file_cand.is_file()
    assert keep.is_dir()


def test_missing_or_dangling_current_fails_before_deletion(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    plan = snapshot_retention_plan(graph, 1)
    (graph / "current").write_text("19990101-000000-missing1\n", encoding="utf-8")
    proc = _run(
        *_prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
    )
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert first.is_dir() and second.is_dir()

    graph2 = tmp_path / "g2"
    keep = _publish(graph2, "only")
    extra = _publish(graph2, "later")
    plan2 = snapshot_retention_plan(graph2, 1)
    (graph2 / "current").unlink()
    proc2 = _run(
        *_prune_args(graph2, 1, plan2["plan_revision"], "--prune-confirmed", "--json")
    )
    assert proc2.returncode != 0
    assert proc2.stdout == ""
    assert keep.is_dir() and extra.is_dir()


def test_malformed_and_unsafe_registry_fails_closed(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    plan = snapshot_retention_plan(graph, 1)
    registry = graph / OPERATOR_PINS_NAME
    before_snaps = (first.is_dir(), second.is_dir())

    registry.write_text('{"schema_version": 1, "pins": [], "pins": []}\n', encoding="utf-8")
    proc = _run(
        *_prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
    )
    assert proc.returncode == 2
    assert proc.stdout == ""

    registry.write_text(
        '{"schema_version": 1, "pins": [], "extra": true}\n', encoding="utf-8"
    )
    assert (
        _run(
            *_prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
        ).returncode
        == 2
    )

    registry.write_bytes(b"x" * (64 * 1024 + 1))
    assert (
        _run(
            *_prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
        ).returncode
        == 2
    )

    registry.unlink()
    registry.symlink_to(tmp_path / "outside.json")
    (tmp_path / "outside.json").write_text(
        canonical_registry_text([]), encoding="utf-8"
    )
    linked = _run(
        *_prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
    )
    assert linked.returncode == 2
    assert "symlink" in linked.stderr.lower()
    registry.unlink()
    registry.mkdir()
    assert (
        _run(
            *_prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
        ).returncode
        == 2
    )
    registry.rmdir()
    assert (first.is_dir(), second.is_dir()) == before_snaps


def test_missing_replaced_symlinked_and_nonregular_lock(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    plan = snapshot_retention_plan(graph, 1)
    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    before_names = {path.name for path in graph.iterdir()}
    missing = _run(
        *_prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
    )
    assert missing.returncode == 2
    assert "adopt-publication-lock" in missing.stderr
    assert not (graph / PUBLICATION_LOCK_NAME).exists()
    assert {path.name for path in graph.iterdir()} == before_names
    assert first.is_dir() and second.is_dir()

    lock.write_bytes(b"lock")
    target = tmp_path / "external.lock"
    target.write_text("untouched", encoding="utf-8")
    lock.unlink()
    lock.symlink_to(target)
    linked = _run(
        *_prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
    )
    assert linked.returncode == 2
    assert "symlink" in linked.stderr.lower()
    assert target.read_text(encoding="utf-8") == "untouched"
    assert first.is_dir() and second.is_dir()

    lock.unlink()
    lock.mkdir()
    nonreg = _run(
        *_prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
    )
    assert nonreg.returncode == 2
    assert lock.is_dir()
    assert first.is_dir() and second.is_dir()


def test_legacy_flat_layout_creates_nothing(tmp_path: Path):
    flat = tmp_path / "flat"
    flat.mkdir()
    pd.DataFrame([{"id": "e"}]).to_parquet(flat / "entities.parquet")
    pd.DataFrame([{"id": "r"}]).to_parquet(flat / "relationships.parquet")
    pd.DataFrame([{"id": "t"}]).to_parquet(flat / "text_units.parquet")
    before = _payload_hashes(flat)
    proc = _run(
        "--graph",
        str(flat),
        "--keep-last",
        "1",
        "--expected-plan-revision",
        "sha256:" + "a" * 64,
        "--prune-confirmed",
        "--json",
    )
    assert proc.returncode == 2
    assert "legacy" in proc.stderr
    assert _payload_hashes(flat) == before
    assert not (flat / OPERATOR_PINS_NAME).exists()
    assert not (flat / PUBLICATION_LOCK_NAME).exists()


def _exclusive_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.byog_graph import graph_exclusive_lease

    try:
        with graph_exclusive_lease(ChildPath(graph)):
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put("held")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _prune_hold(graph: str, keep_last: int, revision: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.snapshot_prune import _snapshot_prune_scope

    try:
        with _snapshot_prune_scope(
            ChildPath(graph),
            keep_last,
            revision,
            prune_confirmed=True,
        ) as result:
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put(("ok", result["ok"], result["deleted_count"]))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _prune_waiter(graph: str, keep_last: int, revision: str, about, got, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import graphrag_code.byog_graph as byog

    orig = byog._acquire_exclusive_lock

    def wrapped(fd):
        about.set()
        backend = orig(fd)
        got.set()
        return backend

    byog._acquire_exclusive_lock = wrapped
    try:
        from graphrag_code.snapshot_prune import snapshot_prune

        result = snapshot_prune(
            ChildPath(graph), keep_last, revision, prune_confirmed=True
        )
        q.put(("ok", result["ok"], result["deleted_count"]))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _publisher(graph: str, marker: str, keep_last: int, about, got, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import pandas as pd
    import graphrag_code.byog_graph as byog

    orig = byog._acquire_exclusive_lock

    def wrapped(fd):
        about.set()
        backend = orig(fd)
        got.set()
        return backend

    byog._acquire_exclusive_lock = wrapped
    ents, rels, tus = [
        [
            {
                "id": f"ent:{marker}",
                "title": f"demo:{marker}",
                "type": "function",
                "source_file": f"{marker}.py",
                "extractor": "tree-sitter-python",
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
    ]
    try:
        snap = byog.publish_byog_snapshot(
            pd.DataFrame(ents),
            pd.DataFrame(rels),
            pd.DataFrame(tus),
            ChildPath(graph),
            keep_last=keep_last,
        )
        q.put(("pub", snap.name))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _pin_waiter(graph: str, snapshot: str, revision: str, about, got, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import graphrag_code.byog_graph as byog

    orig = byog._acquire_exclusive_lock

    def wrapped(fd):
        about.set()
        backend = orig(fd)
        got.set()
        return backend

    byog._acquire_exclusive_lock = wrapped
    try:
        from graphrag_code.snapshot_pins import snapshot_pin

        result = snapshot_pin(
            ChildPath(graph), snapshot, revision, pin_confirmed=True
        )
        q.put(("pin", result["pinned_snapshot"]))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _activate_waiter(
    graph: str, snapshot: str, expected: str, about, got, q
) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import graphrag_code.byog_graph as byog

    orig = byog._acquire_exclusive_lock

    def wrapped(fd):
        about.set()
        backend = orig(fd)
        got.set()
        return backend

    byog._acquire_exclusive_lock = wrapped
    try:
        from graphrag_code.snapshot_activate import snapshot_activate

        result = snapshot_activate(
            ChildPath(graph),
            snapshot,
            expected,
            activate_confirmed=True,
        )
        q.put(("act", result["current"]))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def test_publisher_pin_and_activation_wait_for_prune_lease(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "old")
    second = _publish(graph, "mid")
    plan = snapshot_retention_plan(graph, 2)
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    pruner = CTX.Process(
        target=_prune_hold,
        args=(str(graph), 2, plan["plan_revision"], held, resume, q),
    )
    pub = CTX.Process(target=_publisher, args=(str(graph), "new", 10, about, got, q))
    try:
        pruner.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        listing = {path.name for path in (graph / "snapshots").iterdir()}
        assert first.name in listing or first.name in plan["deletion_candidates"]
        resume.set()
        pub.join(timeout=TIMEOUT)
        pruner.join(timeout=TIMEOUT)
        assert not pub.is_alive() and not pruner.is_alive()
        assert got.is_set()
        assert _current(graph) != second.name or any(
            path.name not in {first.name, second.name}
            for path in (graph / "snapshots").iterdir()
        )
    finally:
        _cleanup_processes(pruner, pub, release=resume)

    pin_target = Path(graph / "snapshots" / _current(graph))
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_exclusive_hold, args=(str(graph), held, resume, q))
    actor = CTX.Process(
        target=_pin_waiter,
        args=(str(graph), pin_target.name, ABSENT_REVISION, about, got, q),
    )
    try:
        holder.start()
        assert held.wait(timeout=TIMEOUT)
        actor.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        assert not (graph / OPERATOR_PINS_NAME).exists()
        resume.set()
        actor.join(timeout=TIMEOUT)
        holder.join(timeout=TIMEOUT)
        assert not actor.is_alive() and not holder.is_alive()
        assert got.is_set()
    finally:
        _cleanup_processes(holder, actor, release=resume)

    older = next(path for path in (graph / "snapshots").iterdir() if path.is_dir())
    current_id = _current(graph)
    if older.name == current_id:
        older = [path for path in (graph / "snapshots").iterdir() if path.name != current_id][0]
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_exclusive_hold, args=(str(graph), held, resume, q))
    actor = CTX.Process(
        target=_activate_waiter,
        args=(str(graph), older.name, current_id, about, got, q),
    )
    try:
        holder.start()
        assert held.wait(timeout=TIMEOUT)
        actor.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        assert _current(graph) == current_id
        resume.set()
        actor.join(timeout=TIMEOUT)
        holder.join(timeout=TIMEOUT)
        assert not actor.is_alive() and not holder.is_alive()
        assert got.is_set()
    finally:
        _cleanup_processes(holder, actor, release=resume)


def test_lock_replacement_while_waiting_fails_closed(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    plan = snapshot_retention_plan(graph, 1)
    before = _protected_state(graph)
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_exclusive_hold, args=(str(graph), held, resume, q))
    waiter = CTX.Process(
        target=_prune_waiter,
        args=(str(graph), 1, plan["plan_revision"], about, got, q),
    )
    try:
        holder.start()
        assert held.wait(timeout=TIMEOUT)
        waiter.start()
        assert about.wait(timeout=TIMEOUT)
        lock = graph / PUBLICATION_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replacement-lock-domain")
        resume.set()
        waiter.join(timeout=TIMEOUT)
        holder.join(timeout=TIMEOUT)
        assert not waiter.is_alive() and not holder.is_alive()
        messages = [q.get(timeout=TIMEOUT), q.get(timeout=TIMEOUT)]
        assert any("publication lock changed" in str(message) for message in messages)
        assert first.is_dir() and second.is_dir()
        assert _current(graph) == before["current"]
        assert tuple(
            sorted(path.name for path in (graph / "snapshots").iterdir())
        ) == before["listing"]
    finally:
        _cleanup_processes(holder, waiter, release=resume)


def test_deterministic_candidate_deletion_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_prune as prune_mod

    graph = tmp_path / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    _publish(graph, "c")
    plan = snapshot_retention_plan(graph, 1)
    seen: list[str] = []
    original = prune_mod._remove_published_snapshot_directory

    def tracked(snapshots_dir, snap_id):
        seen.append(snap_id)
        return original(snapshots_dir, snap_id)

    monkeypatch.setattr(prune_mod, "_remove_published_snapshot_directory", tracked)
    result = snapshot_prune(
        graph, 1, plan["plan_revision"], prune_confirmed=True
    )
    assert seen == plan["deletion_candidates"]
    assert result["deleted_snapshots"] == plan["deletion_candidates"]
    assert seen == sorted(seen, key=lambda item: item.encode("utf-8"))


def test_injected_first_candidate_failure_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_prune as prune_mod

    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    third = _publish(graph, "c")
    plan = snapshot_retention_plan(graph, 1)
    assert len(plan["deletion_candidates"]) >= 2
    before = _protected_state(graph)
    original_remove = prune_mod._remove_published_snapshot_directory

    def boom(snapshots_dir, snap_id):
        raise RuntimeError(f"injected failure on {snap_id}")

    monkeypatch.setattr(prune_mod, "_remove_published_snapshot_directory", boom)
    result = snapshot_prune(
        graph, 1, plan["plan_revision"], prune_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["changed"] is False
    assert result["filesystem_may_have_changed"] is True
    assert result["deleted_snapshots"] == []
    assert result["failed_snapshot"] == plan["deletion_candidates"][0]
    assert result["not_attempted_snapshots"] == plan["deletion_candidates"][1:]
    assert result["retry_requires_fresh_plan"] is True
    assert "injected failure" in (result["error"] or "")
    text = format_result(result)
    assert "PARTIAL FAILURE" in text
    assert "no rollback" in text.lower()
    class Capture:
        def __init__(self) -> None:
            self.chunks: list[str] = []

        def write(self, text: str) -> int:
            self.chunks.append(text)
            return len(text)

        def flush(self) -> None:
            return None

    captured = Capture()
    monkeypatch.setattr(prune_mod.sys, "stdout", captured)
    assert (
        prune_mod.main(
            _prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
        )
        == 1
    )
    body = json.loads("".join(captured.chunks))
    assert body["partial"] is True
    assert body["ok"] is False
    assert body["deleted_snapshots"] == []
    assert first.is_dir() and second.is_dir() and third.is_dir()
    assert _protected_state(graph) == before
    monkeypatch.setattr(
        prune_mod, "_remove_published_snapshot_directory", original_remove
    )

    # A recursive remover can mutate the first candidate and then raise.
    # ``changed`` still counts only fully removed candidate directories;
    # the separate conservative signal must prevent callers from treating
    # that result as a filesystem no-op.
    graph2 = tmp_path / "partially-mutated"
    _publish(graph2, "a")
    _publish(graph2, "b")
    partial_plan = snapshot_retention_plan(graph2, 1)
    failed_id = partial_plan["deletion_candidates"][0]
    failed_dir = graph2 / "snapshots" / failed_id
    victim = failed_dir / "entities.parquet"
    original_rmtree = shutil.rmtree

    def mutate_then_fail(target):
        assert Path(target) == failed_dir
        victim.unlink()
        raise OSError("injected rmtree failure after child removal")

    monkeypatch.setattr(prune_mod.shutil, "rmtree", mutate_then_fail)
    partial_result = snapshot_prune(
        graph2, 1, partial_plan["plan_revision"], prune_confirmed=True
    )
    assert partial_result["ok"] is False
    assert partial_result["partial"] is True
    assert partial_result["changed"] is False
    assert partial_result["filesystem_may_have_changed"] is True
    assert partial_result["deleted_snapshots"] == []
    assert partial_result["failed_snapshot"] == failed_id
    assert failed_dir.is_dir()
    assert not victim.exists()
    monkeypatch.setattr(prune_mod.shutil, "rmtree", original_rmtree)


def test_injected_later_candidate_failure_reports_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_prune as prune_mod

    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    third = _publish(graph, "c")
    plan = snapshot_retention_plan(graph, 1)
    candidates = plan["deletion_candidates"]
    assert len(candidates) >= 2
    original = prune_mod._remove_published_snapshot_directory
    calls = {"n": 0}

    def fail_later(snapshots_dir, snap_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return original(snapshots_dir, snap_id)
        raise RuntimeError(f"injected later failure on {snap_id}")

    monkeypatch.setattr(prune_mod, "_remove_published_snapshot_directory", fail_later)
    result = snapshot_prune(
        graph, 1, plan["plan_revision"], prune_confirmed=True
    )
    assert result["ok"] is False
    assert result["partial"] is True
    assert result["changed"] is True
    assert result["filesystem_may_have_changed"] is True
    assert result["deleted_snapshots"] == [candidates[0]]
    assert result["failed_snapshot"] == candidates[1]
    assert result["not_attempted_snapshots"] == candidates[2:]
    assert result["retry_requires_fresh_plan"] is True
    assert not (graph / "snapshots" / candidates[0]).exists()
    assert (graph / "snapshots" / candidates[1]).is_dir()
    for leftover in candidates[1:]:
        assert (graph / "snapshots" / leftover).is_dir()
    assert third.is_dir()
    assert _current(graph) == third.name
    assert first.name in {candidates[0], candidates[1], third.name}
    assert second.name in {candidates[0], candidates[1], third.name}


def test_cli_serializes_writes_and_flushes_under_exclusive_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from contextlib import contextmanager

    import graphrag_code.snapshot_prune as prune_mod

    graph = tmp_path / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    _publish(graph, "c")
    plan = snapshot_retention_plan(graph, 1)
    original_scope = prune_mod._snapshot_prune_scope
    original_json = prune_mod.result_to_json
    original_format = prune_mod.format_result
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

    monkeypatch.setattr(prune_mod, "_snapshot_prune_scope", tracked_scope)
    monkeypatch.setattr(prune_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(prune_mod, "format_result", guarded_format)
    monkeypatch.setattr(prune_mod.sys, "stdout", GuardedStdout())
    assert (
        prune_mod.main(
            _prune_args(graph, 1, plan["plan_revision"], "--prune-confirmed", "--json")
        )
        == 0
    )

    graph2 = tmp_path / "g2"
    _publish(graph2, "x")
    _publish(graph2, "y")
    _publish(graph2, "z")
    plan2 = snapshot_retention_plan(graph2, 1)
    original = prune_mod._remove_published_snapshot_directory

    def fail_later(snapshots_dir, snap_id):
        if snap_id == plan2["deletion_candidates"][0]:
            return original(snapshots_dir, snap_id)
        raise RuntimeError("injected later")

    monkeypatch.setattr(prune_mod, "_remove_published_snapshot_directory", fail_later)
    assert (
        prune_mod.main(
            _prune_args(graph2, 1, plan2["plan_revision"], "--prune-confirmed")
        )
        == 1
    )
    assert state["active"] is False
    assert state["responses"] >= 2
    assert state["flushes"] == 2


def test_relative_cwd_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    graph = here / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    plan = snapshot_retention_plan(graph, 2)
    args = [
        "--graph",
        "g",
        "--keep-last",
        "2",
        "--expected-plan-revision",
        plan["plan_revision"],
        "--prune-confirmed",
        "--json",
    ]
    # Preview-only parity first: same plan, no deletion (keep_last=2, 2 snaps).
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_prune", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-prune", *args],
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

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-prune",
            "--graph",
            str(graph),
            "--keep-last",
            "2",
            "--expected-plan-revision",
            plan["plan_revision"],
            "--prune-confirmed",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["observed_plan_revision"] == plan["plan_revision"]

    # Relative-cwd mutating prune via the product CLI.
    graph2 = here / "g2"
    older = _publish(graph2, "old")
    newer = _publish(graph2, "new")
    plan2 = snapshot_retention_plan(graph2, 1)
    rel = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "snapshot-prune",
            "--graph",
            "g2",
            "--keep-last",
            "1",
            "--expected-plan-revision",
            plan2["plan_revision"],
            "--prune-confirmed",
            "--json",
        ],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert rel.returncode == 0, rel.stderr
    body = json.loads(rel.stdout)
    assert body["changed"] is True
    assert not older.exists()
    assert newer.is_dir()


def test_mcp_tool_set_remains_exactly_eleven(tmp_path: Path):
    from anyio import run as anyio_run

    graph = tmp_path / "g"
    _publish(graph, "a")
    assert len(TOOL_NAMES) == 12
    assert "snapshot_prune" not in TOOL_NAMES
    assert "snapshot_retention_plan" not in TOOL_NAMES
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 12
            assert "snapshot_prune" not in names

    anyio_run(_body)


def test_implementation_does_not_invoke_producers_or_unrelated_mutations():
    source = MODULE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[-1])
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                called.add(func.id)
            elif isinstance(func, ast.Attribute):
                called.add(func.attr)
    assert (imported | called) & FORBIDDEN == set()
    assert "publish_byog_snapshot" not in source
    assert "snapshot_activate" not in source
    assert "index_python" not in source
    assert "extract_c" not in source
    assert "_publication_lock" not in source
    assert "graph_read_lease" not in source
    assert "subprocess" not in source
    assert "graph_exclusive_lease" in source
    assert "_build_plan_unlocked" in source
    assert "cleanup_old_snapshots" not in imported
    assert "cleanup_old_snapshots" not in called


def test_retained_files_current_registry_and_lock_remain_unchanged(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    snapshot_pin(graph, newer.name, ABSENT_REVISION, pin_confirmed=True)
    plan = snapshot_retention_plan(graph, 1)
    before = _retained_state(graph, plan["retained_snapshots"])
    lock_stat = (graph / PUBLICATION_LOCK_NAME).lstat()
    registry_bytes = (graph / OPERATOR_PINS_NAME).read_bytes()
    result = snapshot_prune(
        graph, 1, plan["plan_revision"], prune_confirmed=True
    )
    assert result["ok"] is True
    assert not older.exists()
    assert newer.is_dir()
    after = _retained_state(graph, plan["retained_snapshots"])
    assert after["current"] == before["current"]
    assert after["registry"] == registry_bytes
    assert after["lock"] == before["lock"]
    lock_after = (graph / PUBLICATION_LOCK_NAME).lstat()
    assert (
        lock_after.st_ino,
        lock_after.st_dev,
        lock_after.st_size,
        lock_after.st_mtime_ns,
        stat.S_IMODE(lock_after.st_mode),
    ) == (
        lock_stat.st_ino,
        lock_stat.st_dev,
        lock_stat.st_size,
        lock_stat.st_mtime_ns,
        stat.S_IMODE(lock_stat.st_mode),
    )
    retained_hashes = {
        key: value
        for key, value in _payload_hashes(graph).items()
        if key.startswith(f"snapshots/{newer.name}/") or key in {"current", OPERATOR_PINS_NAME, PUBLICATION_LOCK_NAME}
    }
    before_hashes = {
        key: value
        for key, value in before["hashes"].items()
        if key.startswith(f"snapshots/{newer.name}/") or key in {"current", OPERATOR_PINS_NAME, PUBLICATION_LOCK_NAME}
    }
    assert retained_hashes == before_hashes


def test_plan_after_success_has_new_revision(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    _publish(graph, "c")
    before = snapshot_retention_plan(graph, 1)
    result = snapshot_prune(
        graph, 1, before["plan_revision"], prune_confirmed=True
    )
    assert result["ok"] is True
    after = snapshot_retention_plan(graph, 1)
    assert after["published_snapshots"] == before["retained_snapshots"]
    assert after["deletion_candidates"] == []
    assert after["plan_revision"] != before["plan_revision"]
    assert after["published_count"] == len(before["retained_snapshots"])
