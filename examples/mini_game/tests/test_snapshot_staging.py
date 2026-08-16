"""Read-only snapshot staging inventory.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_staging.py -q
"""
from __future__ import annotations

import ast
import hashlib
import json
import multiprocessing
import os
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
    STAGING_WRITER_LOCK_NAME,
    probe_staging_writer_lease,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_pins import OPERATOR_PINS_NAME  # type: ignore
from graphrag_code.snapshot_staging import (  # type: ignore
    MAX_CURRENT_BYTES,
    MAX_PUBLISHED_SNAPSHOTS,
    MAX_STAGING_ENTRIES,
    MAX_TOP_LEVEL_ENTRIES,
    SnapshotStagingError,
    SnapshotStagingIntegrityError,
    canonical_staging_revision_text,
    format_result,
    result_to_json,
    snapshot_staging,
    staging_revision_of,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
SCRIPT = ROOT / "scripts" / "snapshot_staging.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_staging.py"
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
        "c_clang_ast_capture",
        "c_compiler_facts",
        "graph_exclusive_lease",
        "_publication_lock",
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
        settings_text=f"staging-inventory: {marker}\n",
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


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )


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


def _entry(result: dict, name: str) -> dict:
    matches = [item for item in result["staging_entries"] if item["name"] == name]
    assert len(matches) == 1, name
    return matches[0]


def test_empty_staging_inventory(tmp_path: Path):
    graph = tmp_path / "g"
    published = _publish(graph, "only")
    before = _protected_state(graph)
    result = snapshot_staging(graph)
    assert result["schema_version"] == 2
    assert result["ok"] is True
    assert result["graph"] == str(graph.resolve())
    assert result["current"] == published.name
    assert result["published_snapshots"] == [published.name]
    assert result["published_count"] == 1
    assert result["staging_count"] == 0
    assert result["staging_entries"] == []
    assert result["ownership_inference"] is False
    assert result["cleanup_supported"] is False
    assert result["staging_revision"].startswith("sha256:")
    assert len(result["staging_revision"]) == len("sha256:") + 64
    codes = [notice["code"] for notice in result["notices"]]
    assert codes == [
        "ownership_unknown",
        "cleanup_not_supported",
        "staging_not_leased",
        "writer_lease_not_ownership",
    ]
    assert _protected_state(graph) == before


def test_structurally_complete_staging_candidate(tmp_path: Path):
    graph = tmp_path / "g"
    live = _publish(graph, "live")
    staging = _staging_dir(graph, live.name)
    _write_complete_payload(staging)
    result = snapshot_staging(graph)
    entry = _entry(result, staging.name)
    assert result["staging_count"] == 1
    assert entry["candidate_snapshot_id"] == live.name
    assert entry["name_valid"] is True
    assert entry["entry_kind"] == "directory"
    assert entry["top_level_entry_count"] == 6
    assert [item["name"] for item in entry["top_level_entries"]] == sorted(
        [
            "call_observations.parquet",
            "entities.parquet",
            "manifest.json",
            "relationships.parquet",
            "settings.yaml",
            "text_units.parquet",
        ],
        key=lambda item: item.encode("utf-8"),
    )
    assert entry["has_manifest_json"] is True
    assert entry["has_entities_parquet"] is True
    assert entry["has_relationships_parquet"] is True
    assert entry["has_text_units_parquet"] is True
    assert entry["has_call_observations_parquet"] is True
    assert entry["has_settings_yaml"] is True
    assert entry["complete_payload_candidate"] is True
    assert entry["writer_lease_protocol"] == "legacy_absent"
    assert entry["writer_lease_state"] == "unverifiable"
    assert entry["writer_lock_present"] is False
    assert entry["writer_lock_regular"] is False
    assert entry["ownership_status"] == "unknown"
    assert entry["cleanup_eligible"] is False
    assert any(notice["code"] == "writer_lock_legacy_absent" for notice in entry["notices"])
    assert staging.is_dir()
    assert live.is_dir()


def test_incomplete_staging_candidates(tmp_path: Path):
    graph = tmp_path / "g"
    live = _publish(graph, "live")
    missing = _staging_dir(graph, "20240101-000000-incomplete")
    (missing / "manifest.json").write_text("{}\n", encoding="utf-8")
    (missing / "entities.parquet").write_bytes(b"ENT")
    result = snapshot_staging(graph)
    entry = _entry(result, missing.name)
    assert entry["candidate_snapshot_id"] == "20240101-000000-incomplete"
    assert entry["name_valid"] is True
    assert entry["complete_payload_candidate"] is False
    assert entry["has_manifest_json"] is True
    assert entry["has_entities_parquet"] is True
    assert entry["has_relationships_parquet"] is False
    assert entry["has_text_units_parquet"] is False
    assert entry["writer_lease_protocol"] == "legacy_absent"
    assert entry["writer_lease_state"] == "unverifiable"
    assert entry["ownership_status"] == "unknown"
    assert entry["cleanup_eligible"] is False
    codes = [notice["code"] for notice in entry["notices"]]
    assert "incomplete_payload_shape" in codes
    assert "writer_lock_legacy_absent" in codes
    assert live.is_dir() and missing.is_dir()


def test_malformed_staging_suffix(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    empty = _staging_dir(graph, "")
    hidden = _staging_dir(graph, ".hidden")
    dots = _staging_dir(graph, "..")
    result = snapshot_staging(graph)
    assert result["staging_count"] == 3
    names = [entry["name"] for entry in result["staging_entries"]]
    assert names == sorted(names, key=lambda item: item.encode("utf-8"))
    for name in (empty.name, hidden.name, dots.name):
        entry = _entry(result, name)
        assert entry["candidate_snapshot_id"] is None
        assert entry["name_valid"] is False
        assert entry["complete_payload_candidate"] is False
        assert any(notice["code"] == "name_not_canonical" for notice in entry["notices"])


def test_canonical_utf8_ordering(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    later = _staging_dir(graph, "zzzzzzzz-000000-ffffffff")
    earlier = _staging_dir(graph, "aaaaaaaa-000000-ffffffff")
    mid = _staging_dir(graph, "mmmmmmmm-000000-ffffffff")
    result = snapshot_staging(graph)
    names = [entry["name"] for entry in result["staging_entries"]]
    expected = sorted(
        [later.name, earlier.name, mid.name], key=lambda item: item.encode("utf-8")
    )
    assert names == expected
    assert result["published_snapshots"] == sorted(
        result["published_snapshots"], key=lambda item: item.encode("utf-8")
    )


def test_staging_entry_symlink_refused_without_following(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = tmp_path / "g"
    _publish(graph, "live")
    target = tmp_path / "outside-staging"
    target.mkdir()
    (target / "secret").write_text("do-not-read", encoding="utf-8")
    linked = graph / "snapshots" / f"{STAGING_NAME_PREFIX}20240101-000000-symlink"
    linked.symlink_to(target)
    before_target = target.stat()
    proc = _run("--graph", str(graph), "--json")
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "symlink" in proc.stderr.lower()
    assert (target / "secret").read_text(encoding="utf-8") == "do-not-read"
    assert target.stat().st_mtime_ns == before_target.st_mtime_ns
    assert linked.is_symlink()

    # Close the lstat/open race: replacing a previously inspected directory
    # with a symlink immediately before os.open must hit O_NOFOLLOW and must
    # never reach descriptor-relative scandir on the outside target.
    import graphrag_code.snapshot_staging as staging_mod

    graph2 = tmp_path / "g2"
    _publish(graph2, "live")
    raced = _staging_dir(graph2, "20240101-000000-racedlink")
    (raced / "local").write_text("local", encoding="utf-8")
    parked = tmp_path / "parked-staging"
    outside = tmp_path / "outside-race"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("must-not-be-scanned", encoding="utf-8")
    original_open = os.open
    swapped = {"done": False}

    def swap_before_open(path, flags, *args, **kwargs):
        if Path(path) == raced and not swapped["done"]:
            swapped["done"] = True
            raced.rename(parked)
            raced.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(staging_mod.os, "open", swap_before_open)
    with pytest.raises(SnapshotStagingIntegrityError, match="changed|unsafe"):
        snapshot_staging(graph2)
    assert swapped["done"] is True
    assert raced.is_symlink()
    assert secret.read_text(encoding="utf-8") == "must-not-be-scanned"
    assert (parked / "local").read_text(encoding="utf-8") == "local"


def test_top_level_child_symlink_refused_without_following(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    staging = _staging_dir(graph, "20240101-000000-childlink")
    target = tmp_path / "outside-child"
    target.write_text("payload", encoding="utf-8")
    (staging / "manifest.json").symlink_to(target)
    before = target.stat()
    proc = _run("--graph", str(graph), "--json")
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "symlink" in proc.stderr.lower()
    assert target.read_text(encoding="utf-8") == "payload"
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert (staging / "manifest.json").is_symlink()


def test_nested_and_nonregular_entry_policy(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    staging = _staging_dir(graph, "20240101-000000-policy")
    _write_complete_payload(staging)
    nested = staging / "nested"
    nested.mkdir()
    (nested / "hidden.bin").write_bytes(b"deep")
    fifo = staging / "extra.fifo"
    os.mkfifo(fifo)
    extra = staging / "notes.txt"
    extra.write_text("extra\n", encoding="utf-8")
    result = snapshot_staging(graph)
    entry = _entry(result, staging.name)
    kinds = {item["name"]: item["entry_kind"] for item in entry["top_level_entries"]}
    assert kinds["nested"] == "directory"
    assert kinds["extra.fifo"] == "fifo"
    assert kinds["notes.txt"] == "file"
    assert "hidden.bin" not in kinds
    codes = {notice["code"] for notice in entry["notices"]}
    assert "nested_directory" in codes
    assert "non_regular_entry" in codes
    assert "unexpected_top_level_entry" in codes
    assert entry["complete_payload_candidate"] is True
    assert entry["cleanup_eligible"] is False
    assert entry["ownership_status"] == "unknown"
    assert fifo.exists()
    assert (nested / "hidden.bin").read_bytes() == b"deep"


def test_hard_resource_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    graph = tmp_path / "g"
    _publish(graph, "live")
    for index in range(MAX_STAGING_ENTRIES + 1):
        _staging_dir(graph, f"20240101-000000-{index:08x}")
    too_many = _run("--graph", str(graph), "--json")
    assert too_many.returncode == 2
    assert too_many.stdout == ""
    assert "exceeds bound" in too_many.stderr

    other = tmp_path / "g2"
    _publish(other, "live")
    wide = _staging_dir(other, "20240101-000000-aaaaaaaa")
    for index in range(MAX_TOP_LEVEL_ENTRIES + 1):
        (wide / f"file-{index:02d}.bin").write_bytes(b"x")
    too_wide = _run("--graph", str(other), "--json")
    assert too_wide.returncode == 2
    assert too_wide.stdout == ""
    assert "exceeds bound" in too_wide.stderr

    import graphrag_code.snapshot_staging as staging_mod

    published_graph = tmp_path / "published-bound"
    for marker in ("one", "two", "three"):
        _publish(published_graph, marker)
    monkeypatch.setattr(staging_mod, "MAX_PUBLISHED_SNAPSHOTS", 2)
    with pytest.raises(SnapshotStagingError, match="published snapshot count"):
        snapshot_staging(published_graph)

    monkeypatch.setattr(staging_mod.os, "supports_fd", set())
    with pytest.raises(SnapshotStagingError, match="descriptor-relative"):
        snapshot_staging(other)

    current_bound = tmp_path / "current-bound"
    _publish(current_bound, "live")
    (current_bound / "current").write_bytes(b"x" * (MAX_CURRENT_BYTES + 1))
    oversized_current = _run("--graph", str(current_bound), "--json")
    assert oversized_current.returncode == 2
    assert oversized_current.stdout == ""
    assert "current pointer exceeds bound" in oversized_current.stderr


def _mutate_after_first(original, action):
    seen = {"n": 0}

    def wrapped(path: Path):
        result = original(path)
        seen["n"] += 1
        if seen["n"] == 1:
            action(path)
        return result

    return wrapped


def test_two_scan_detects_staging_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import shutil

    import graphrag_code.snapshot_staging as staging_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    first = _staging_dir(graph, "aaaaaaaa-000000-ffffffff")
    _write_complete_payload(first)
    original = staging_mod._scan_inventory_state

    added = graph / "snapshots" / f"{STAGING_NAME_PREFIX}bbbbbbbb-000000-ffffffff"
    monkeypatch.setattr(
        staging_mod,
        "_scan_inventory_state",
        _mutate_after_first(original, lambda _root: added.mkdir()),
    )
    with pytest.raises(SnapshotStagingIntegrityError, match="staging_names"):
        snapshot_staging(graph)
    added.rmdir()

    parked = graph / "snapshots" / f"{STAGING_NAME_PREFIX}removed-temp"

    def remove_first(_root: Path) -> None:
        first.rename(parked)

    monkeypatch.setattr(
        staging_mod,
        "_scan_inventory_state",
        _mutate_after_first(original, remove_first),
    )
    with pytest.raises(SnapshotStagingIntegrityError, match="staging_names"):
        snapshot_staging(graph)
    parked.rename(first)

    def replace_identity(_root: Path) -> None:
        parked_dir = tmp_path / "replacement-old"
        first.rename(parked_dir)
        first.mkdir()
        _write_complete_payload(first)

    monkeypatch.setattr(
        staging_mod,
        "_scan_inventory_state",
        _mutate_after_first(original, replace_identity),
    )
    with pytest.raises(
        SnapshotStagingIntegrityError, match="staging_identity|staging_content_metadata"
    ):
        snapshot_staging(graph)
    shutil.rmtree(first)
    (tmp_path / "replacement-old").rename(first)

    def change_type(_root: Path) -> None:
        shutil.rmtree(first)
        first.write_text("not-a-dir", encoding="utf-8")

    monkeypatch.setattr(
        staging_mod,
        "_scan_inventory_state",
        _mutate_after_first(original, change_type),
    )
    with pytest.raises(SnapshotStagingIntegrityError, match="staging_type"):
        snapshot_staging(graph)
    first.unlink()
    first.mkdir()
    _write_complete_payload(first)

    def change_bytes(_root: Path) -> None:
        (first / "entities.parquet").write_bytes(b"CHANGED")

    monkeypatch.setattr(
        staging_mod,
        "_scan_inventory_state",
        _mutate_after_first(original, change_bytes),
    )
    with pytest.raises(SnapshotStagingIntegrityError, match="staging_content_metadata"):
        snapshot_staging(graph)


def test_writer_lock_symlink_and_nonregular_fail_closed(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    staging = _staging_dir(graph, "20240101-000000-locklink")
    target = tmp_path / "outside-writer.lock"
    target.write_bytes(b"secret")
    (staging / STAGING_WRITER_LOCK_NAME).symlink_to(target)
    before = target.stat()
    proc = _run("--graph", str(graph), "--json")
    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "symlink" in proc.stderr.lower()
    assert target.read_bytes() == b"secret"
    assert target.stat().st_mtime_ns == before.st_mtime_ns
    assert (staging / STAGING_WRITER_LOCK_NAME).is_symlink()

    other = tmp_path / "g2"
    _publish(other, "live")
    fifo_dir = _staging_dir(other, "20240101-000000-lockfifo")
    os.mkfifo(fifo_dir / STAGING_WRITER_LOCK_NAME)
    fifo = _run("--graph", str(other), "--json")
    assert fifo.returncode == 1
    assert fifo.stdout == ""
    assert "writer lock" in fifo.stderr.lower() or "non-regular" in fifo.stderr.lower()


def test_writer_lock_probe_does_not_mutate_metadata(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "live")
    staging = _staging_dir(graph, "20240101-000000-probe")
    _write_complete_payload(staging)
    lock = staging / STAGING_WRITER_LOCK_NAME
    lock.write_bytes(b"")
    before = lock.lstat()
    before_bytes = lock.read_bytes()
    result = snapshot_staging(graph)
    entry = _entry(result, staging.name)
    assert entry["writer_lease_protocol"] == "cooperative_v1"
    assert entry["writer_lease_state"] == "not_held_at_scan"
    assert entry["writer_lock_present"] is True
    assert entry["writer_lock_regular"] is True
    assert entry["ownership_status"] == "unknown"
    assert entry["cleanup_eligible"] is False
    after = lock.lstat()
    assert lock.read_bytes() == before_bytes
    assert after.st_ino == before.st_ino
    assert after.st_dev == before.st_dev
    assert stat.S_IMODE(after.st_mode) == stat.S_IMODE(before.st_mode)
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns


def test_two_scan_detects_writer_lease_state_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_staging as staging_mod

    graph = tmp_path / "g"
    _publish(graph, "live")
    staging = _staging_dir(graph, "aaaaaaaa-000000-ffffffff")
    (staging / STAGING_WRITER_LOCK_NAME).write_bytes(b"")
    original = staging_mod._scan_inventory_state
    q = CTX.Queue()
    held = CTX.Event()
    resume = CTX.Event()
    holder = CTX.Process(
        target=_writer_lease_hold, args=(str(staging), held, resume, q)
    )
    try:
        monkeypatch.setattr(
            staging_mod,
            "_scan_inventory_state",
            _mutate_after_first(
                original,
                lambda _root: (
                    holder.start(),
                    held.wait(timeout=TIMEOUT),
                ),
            ),
        )
        with pytest.raises(SnapshotStagingIntegrityError, match="writer_lease_state"):
            snapshot_staging(graph)
    finally:
        _cleanup_processes(holder, release=resume)


def test_writer_lock_identity_and_open_races(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.byog_graph as byog

    graph = tmp_path / "g"
    _publish(graph, "live")
    staging = _staging_dir(graph, "20240101-000000-lockrace")
    lock = staging / STAGING_WRITER_LOCK_NAME
    lock.write_bytes(b"")
    parked = tmp_path / "parked-writer.lock"
    outside = tmp_path / "outside-writer.lock"
    outside.write_bytes(b"must-not-follow")
    original_open = os.open
    swapped = {"done": False}

    def swap_lock_before_open(path, flags, *args, **kwargs):
        if Path(path) == lock and not swapped["done"]:
            swapped["done"] = True
            lock.rename(parked)
            lock.symlink_to(outside)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(byog.os, "open", swap_lock_before_open)
    with pytest.raises(SnapshotStagingIntegrityError, match="unsafe|changed|symlink"):
        snapshot_staging(graph)
    assert swapped["done"] is True
    assert lock.is_symlink()
    assert outside.read_bytes() == b"must-not-follow"
    assert parked.read_bytes() == b""

    graph2 = tmp_path / "g2"
    _publish(graph2, "live")
    staging2 = _staging_dir(graph2, "20240101-000000-missingopen")
    lock2 = staging2 / STAGING_WRITER_LOCK_NAME
    lock2.write_bytes(b"")
    original_open2 = os.open
    unlinked = {"done": False}

    def unlink_before_open(path, flags, *args, **kwargs):
        if Path(path) == lock2 and not unlinked["done"]:
            unlinked["done"] = True
            lock2.unlink()
        return original_open2(path, flags, *args, **kwargs)

    monkeypatch.setattr(byog.os, "open", unlink_before_open)
    with pytest.raises(
        SnapshotStagingIntegrityError, match="disappeared|changed|unsafe"
    ):
        snapshot_staging(graph2)
    assert unlinked["done"] is True
    assert not lock2.exists()

    graph3 = tmp_path / "g3"
    _publish(graph3, "live")
    staging3 = _staging_dir(graph3, "20240101-000000-inodeswap")
    lock3 = staging3 / STAGING_WRITER_LOCK_NAME
    lock3.write_bytes(b"")
    original_open3 = os.open
    swapped_inode = {"done": False}

    def replace_inode_before_open(path, flags, *args, **kwargs):
        if Path(path) == lock3 and not swapped_inode["done"]:
            swapped_inode["done"] = True
            lock3.unlink()
            lock3.write_bytes(b"replacement")
        return original_open3(path, flags, *args, **kwargs)

    monkeypatch.setattr(byog.os, "open", replace_inode_before_open)
    with pytest.raises(SnapshotStagingIntegrityError, match="changed|unsafe"):
        snapshot_staging(graph3)
    assert swapped_inode["done"] is True


def test_publisher_promotion_waits_for_response_lease(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "old")
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    inventory = CTX.Process(
        target=_inventory_hold, args=(str(graph), held, resume, q)
    )
    pub = CTX.Process(target=_publisher, args=(str(graph), "new", 10, about, got, q))
    try:
        inventory.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        assert _current(graph) == first.name
        staging = [
            path
            for path in (graph / "snapshots").iterdir()
            if path.name.startswith(STAGING_NAME_PREFIX)
        ]
        assert len(staging) == 1
        assert (
            probe_staging_writer_lease(staging[0])["writer_lease_state"]
            == "held_by_cooperating_writer"
        )
        resume.set()
        pub.join(timeout=TIMEOUT)
        inventory.join(timeout=TIMEOUT)
        assert not pub.is_alive() and not inventory.is_alive()
        assert got.is_set()
        assert _current(graph) != first.name
    finally:
        _cleanup_processes(inventory, pub, release=resume)


def test_missing_replaced_symlinked_and_nonregular_lock(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    before_names = {path.name for path in graph.iterdir()}
    missing = _run("--graph", str(graph), "--json")
    assert missing.returncode == 2
    assert "adopt-publication-lock" in missing.stderr
    assert missing.stdout == ""
    assert not (graph / PUBLICATION_LOCK_NAME).exists()
    assert {path.name for path in graph.iterdir()} == before_names

    _publish(graph, "b")
    lock.unlink()
    target = tmp_path / "external.lock"
    target.write_text("untouched", encoding="utf-8")
    lock.symlink_to(target)
    linked = _run("--graph", str(graph), "--json")
    assert linked.returncode == 2
    assert "symlink" in linked.stderr.lower()
    assert linked.stdout == ""
    assert target.read_text(encoding="utf-8") == "untouched"

    lock.unlink()
    lock.mkdir()
    nonreg = _run("--graph", str(graph), "--json")
    assert nonreg.returncode == 2
    assert nonreg.stdout == ""
    assert lock.is_dir()


def test_lock_replacement_while_inventory_waits_fails_closed(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    before = _protected_state(graph)
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_exclusive_hold, args=(str(graph), held, resume, q))
    waiter = CTX.Process(target=_inventory_waiter, args=(str(graph), about, got, q))
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
        assert _current(graph) == before["current"]
        assert tuple(
            sorted(path.name for path in (graph / "snapshots").iterdir())
        ) == before["listing"]
    finally:
        _cleanup_processes(holder, waiter, release=resume)


def test_legacy_flat_and_incomplete_managed_layout(tmp_path: Path):
    flat = tmp_path / "flat"
    flat.mkdir()
    pd.DataFrame([{"id": "e"}]).to_parquet(flat / "entities.parquet")
    pd.DataFrame([{"id": "r"}]).to_parquet(flat / "relationships.parquet")
    pd.DataFrame([{"id": "t"}]).to_parquet(flat / "text_units.parquet")
    before = _payload_hashes(flat)
    proc = _run("--graph", str(flat), "--json")
    assert proc.returncode == 2
    assert "legacy" in proc.stderr
    assert proc.stdout == ""
    assert _payload_hashes(flat) == before
    assert not (flat / PUBLICATION_LOCK_NAME).exists()

    incomplete = tmp_path / "incomplete"
    incomplete.mkdir()
    (incomplete / "current").write_text("20240101-000000-deadbeef\n", encoding="utf-8")
    missing_snapshots = _run("--graph", str(incomplete), "--json")
    assert missing_snapshots.returncode == 2
    assert "incomplete" in missing_snapshots.stderr
    assert missing_snapshots.stdout == ""
    assert not (incomplete / "snapshots").exists()

    dangling = tmp_path / "dangling"
    live = _publish(dangling, "live")
    (dangling / "current").write_text("20240101-000000-missing1\n", encoding="utf-8")
    missing_current = _run("--graph", str(dangling), "--json")
    assert missing_current.returncode == 1
    assert missing_current.stdout == ""
    assert "dangling" in missing_current.stderr or "missing" in missing_current.stderr
    assert live.is_dir()


def test_symlinked_graph_snapshots_and_current_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    graph = tmp_path / "g"
    live = _publish(graph, "live")
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(graph)
    root = _run("--graph", str(linked_root), "--json")
    assert root.returncode == 2
    assert root.stdout == ""
    assert "symlink" in root.stderr.lower()

    snapshots = graph / "snapshots"
    parked = tmp_path / "real-snapshots"
    snapshots.rename(parked)
    snapshots.symlink_to(parked)
    snaps = _run("--graph", str(graph), "--json")
    assert snaps.returncode == 2
    assert snaps.stdout == ""
    assert "symlink" in snaps.stderr.lower()
    snapshots.unlink()
    parked.rename(snapshots)

    current = graph / "current"
    target = tmp_path / "current-target"
    target.write_text(live.name + "\n", encoding="utf-8")
    current.unlink()
    current.symlink_to(target)
    pointer = _run("--graph", str(graph), "--json")
    assert pointer.returncode == 2
    assert pointer.stdout == ""
    assert "symlink" in pointer.stderr.lower()
    assert target.read_text(encoding="utf-8") == live.name + "\n"

    import graphrag_code.snapshot_staging as staging_mod

    graph2 = tmp_path / "current-race"
    live2 = _publish(graph2, "live")
    current2 = graph2 / "current"
    parked_current = tmp_path / "parked-current"
    outside_current = tmp_path / "outside-current"
    outside_current.write_text(live2.name + "\n", encoding="utf-8")
    original_open = os.open
    swapped = {"done": False}

    def swap_current_before_open(path, flags, *args, **kwargs):
        if Path(path) == current2 and not swapped["done"]:
            swapped["done"] = True
            current2.rename(parked_current)
            current2.symlink_to(outside_current)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(staging_mod.os, "open", swap_current_before_open)
    with pytest.raises(SnapshotStagingIntegrityError, match="changed|unsafe"):
        snapshot_staging(graph2)
    assert swapped["done"] is True
    assert current2.is_symlink()
    assert parked_current.read_text(encoding="utf-8") == live2.name
    assert outside_current.read_text(encoding="utf-8") == live2.name + "\n"


def test_relative_cwd_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    graph = here / "g"
    _publish(graph, "a")
    staging = _staging_dir(graph, "20240101-000000-relcwd")
    _write_complete_payload(staging)
    args = ["--graph", "g", "--json"]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_staging", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-staging", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    bodies = [json.loads(proc.stdout) for proc in (module, script, cli)]
    assert bodies[0] == bodies[1] == bodies[2]
    listed = snapshot_staging(graph)
    assert bodies[0]["staging_revision"] == listed["staging_revision"]
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        ["graphrag-code", "snapshot-staging", "--graph", str(graph), "--json"],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["staging_revision"] == listed["staging_revision"]

    sdist = built_wheel_and_sdist[1]
    import tarfile

    with tarfile.open(sdist, "r:gz") as tf:
        names = "\n".join(tf.getnames())
    assert "snapshot_staging.py" in names


def test_deterministic_json_and_plain_text(tmp_path: Path):
    graph = tmp_path / "g"
    live = _publish(graph, "a")
    staging = _staging_dir(graph, live.name)
    _write_complete_payload(staging)
    first = snapshot_staging(graph)
    second = snapshot_staging(graph)
    assert first == second
    assert first["staging_revision"] == staging_revision_of(first)
    expected = (
        "sha256:"
        + hashlib.sha256(
            canonical_staging_revision_text(first).encode("utf-8")
        ).hexdigest()
    )
    assert first["staging_revision"] == expected
    payload = json.loads(canonical_staging_revision_text(first))
    assert "graph" not in payload
    assert "notices" not in payload
    assert "ownership_inference" not in payload
    assert payload["schema_version"] == 2
    assert payload["current"] == first["current"]
    assert payload["published_snapshots"] == first["published_snapshots"]
    assert payload["staging_entries"] == first["staging_entries"]
    text = format_result(first)
    assert text.startswith("snapshot-staging:")
    assert first["staging_revision"] in text
    lowered = text.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered
    encoded = result_to_json(first)
    assert encoded == result_to_json(second)
    assert encoded.endswith("\n")
    parsed = json.loads(encoded)
    assert list(parsed) == sorted(parsed)


def test_cli_serializes_writes_and_flushes_under_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from contextlib import contextmanager

    import graphrag_code.snapshot_staging as staging_mod

    graph = tmp_path / "g"
    _publish(graph, "a")
    original_scope = staging_mod._snapshot_staging_scope
    original_json = staging_mod.result_to_json
    original_format = staging_mod.format_result
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

    monkeypatch.setattr(staging_mod, "_snapshot_staging_scope", tracked_scope)
    monkeypatch.setattr(staging_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(staging_mod, "format_result", guarded_format)
    monkeypatch.setattr(staging_mod.sys, "stdout", GuardedStdout())
    assert staging_mod.main(["--graph", str(graph), "--json"]) == 0
    assert staging_mod.main(["--graph", str(graph)]) == 0
    assert state["active"] is False
    assert state["responses"] >= 2
    assert state["flushes"] == 2


def test_successful_inventory_leaves_graph_unchanged(tmp_path: Path):
    graph = tmp_path / "g"
    live = _publish(graph, "live")
    staging = _staging_dir(graph, live.name)
    _write_complete_payload(staging)
    before = _protected_state(graph)
    ok = snapshot_staging(graph)
    assert ok["staging_count"] == 1
    assert _protected_state(graph) == before

    missing = _run("--json")
    assert missing.returncode == 2
    assert missing.stdout == ""
    assert _protected_state(graph) == before

    with pytest.raises(SnapshotStagingError):
        snapshot_staging(tmp_path / "missing")
    assert _protected_state(graph) == before
    assert "Traceback" not in missing.stderr


def test_mcp_tool_set_remains_exactly_eleven(tmp_path: Path):
    from anyio import run as anyio_run

    graph = tmp_path / "g"
    _publish(graph, "a")
    assert len(TOOL_NAMES) == 11
    assert "snapshot_staging" not in TOOL_NAMES
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 11
            assert "snapshot_staging" not in names

    anyio_run(_body)


def test_implementation_does_not_invoke_producers_or_mutations():
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
    assert "snapshot_prune" not in source
    assert "index_python" not in source
    assert "extract_c" not in source
    assert "graph_exclusive_lease" not in source
    assert "graph_read_lease" in source
    lowered = source.lower()
    for word in FORBIDDEN_WORDS:
        assert word not in lowered


def _writer_lease_hold(stage_dir: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.byog_graph import staging_writer_lease

    try:
        with staging_writer_lease(ChildPath(stage_dir)):
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put("held")
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


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


def _inventory_hold(graph: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.snapshot_staging import _snapshot_staging_scope

    try:
        with _snapshot_staging_scope(ChildPath(graph)) as result:
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put(("ok", result["staging_revision"]))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _inventory_waiter(graph: str, about, got, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import graphrag_code.byog_graph as byog

    orig = byog._acquire_lock

    def wrapped(fd, *, exclusive):
        about.set()
        backend = orig(fd, exclusive=exclusive)
        got.set()
        return backend

    byog._acquire_lock = wrapped
    try:
        from graphrag_code.snapshot_staging import snapshot_staging as inventory

        result = inventory(ChildPath(graph))
        q.put(("ok", result["staging_revision"]))
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
    ents = [
        {
            "id": f"ent:{marker}",
            "title": f"demo:{marker}",
            "type": "function",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
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
