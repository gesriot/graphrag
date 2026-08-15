"""Operator-managed snapshot retention pins.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_pins.py -q
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
    cleanup_old_snapshots,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_pins import (  # type: ignore
    ABSENT_REVISION,
    OPERATOR_PINS_NAME,
    SnapshotPinsError,
    SnapshotPinsIntegrityError,
    canonical_registry_text,
    result_to_json,
    revision_of_bytes,
    snapshot_pin,
    snapshot_pins_list,
    snapshot_unpin,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
PINS = ROOT / "scripts" / "snapshot_pins.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_pins.py"
FORBIDDEN = frozenset(
    {
        "index_python",
        "index_c",
        "extract_c",
        "extract_python",
        "publish_byog_snapshot",
        "cleanup_old_snapshots",
        "snapshot_activate",
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
        settings_text=f"pins: {marker}\n",
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
    return {
        "hashes": {
            key: value
            for key, value in _payload_hashes(graph).items()
            if key != OPERATOR_PINS_NAME
        },
        "stats": {
            key: value
            for key, value in _payload_stats(graph).items()
            if key != OPERATOR_PINS_NAME
        },
        "current": _current(graph),
        "listing": tuple(sorted(path.name for path in (graph / "snapshots").iterdir())),
        "lock": (graph / PUBLICATION_LOCK_NAME).read_bytes(),
        "lock_stat": (
            (graph / PUBLICATION_LOCK_NAME).lstat().st_ino,
            (graph / PUBLICATION_LOCK_NAME).lstat().st_dev,
            (graph / PUBLICATION_LOCK_NAME).lstat().st_size,
            (graph / PUBLICATION_LOCK_NAME).lstat().st_mtime_ns,
        ),
    }


def _revision_of(graph: Path) -> str:
    path = graph / OPERATOR_PINS_NAME
    if not path.exists():
        return ABSENT_REVISION
    return revision_of_bytes(path.read_bytes())


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PINS), *args],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        env=_child_env(),
    )


def test_absent_registry_listing_is_read_only(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a")
    before = _protected_state(graph)
    listed = snapshot_pins_list(graph)
    assert listed["registry_revision"] == ABSENT_REVISION
    assert listed["schema_version"] == 1
    assert listed["graph"] == str(graph.resolve())
    assert listed["current"] == first.name
    assert listed["operator_pins"] == []
    assert listed["effective_pins"] == listed["claim_pins"]
    assert not (graph / OPERATOR_PINS_NAME).exists()
    assert _protected_state(graph) == before


def test_canonical_pin_idempotent_pin_unpin_and_empty_registry(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    before = _protected_state(graph)
    created = snapshot_pin(
        graph, older.name, ABSENT_REVISION, pin_confirmed=True
    )
    assert created["ok"] is True
    assert created["changed"] is True
    assert created["pinned_snapshot"] == older.name
    assert created["operator_pins"] == [older.name]
    assert older.name in created["effective_pins"]
    assert created["previous_registry_revision"] == ABSENT_REVISION
    assert created["registry_revision"].startswith("sha256:")
    expected_bytes = canonical_registry_text([older.name]).encode("utf-8")
    assert (graph / OPERATOR_PINS_NAME).read_bytes() == expected_bytes
    assert created["registry_revision"] == revision_of_bytes(expected_bytes)
    after_create = _protected_state(graph)
    assert after_create["current"] == before["current"] == newer.name
    assert after_create["hashes"] == before["hashes"]
    assert after_create["stats"] == before["stats"]
    assert after_create["listing"] == before["listing"]
    assert after_create["lock"] == before["lock"]
    assert after_create["lock_stat"] == before["lock_stat"]

    stats_before_idempotent = _payload_stats(graph)[OPERATOR_PINS_NAME]
    again = snapshot_pin(
        graph, older.name, created["registry_revision"], pin_confirmed=True
    )
    assert again["changed"] is False
    assert again["registry_revision"] == created["registry_revision"]
    assert (graph / OPERATOR_PINS_NAME).read_bytes() == expected_bytes
    assert _payload_stats(graph)[OPERATOR_PINS_NAME] == stats_before_idempotent

    removed = snapshot_unpin(
        graph, older.name, again["registry_revision"], unpin_confirmed=True
    )
    assert removed["changed"] is True
    assert removed["immediate_deletion"] is False
    assert removed["eligible_for_future_retention"] is True
    assert "no immediate deletion" in removed["retention_effect"]
    empty = canonical_registry_text([]).encode("utf-8")
    assert (graph / OPERATOR_PINS_NAME).is_file()
    assert (graph / OPERATOR_PINS_NAME).read_bytes() == empty
    assert removed["operator_pins"] == []
    assert older.is_dir()
    assert _current(graph) == newer.name

    noop = snapshot_unpin(
        graph, older.name, removed["registry_revision"], unpin_confirmed=True
    )
    assert noop["changed"] is False
    assert noop["immediate_deletion"] is False
    assert (graph / OPERATOR_PINS_NAME).read_bytes() == empty
    assert _protected_state(graph)["hashes"] == before["hashes"]


def test_confirmation_and_expected_revision_are_mandatory(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    _publish(graph, "newer")
    before = _protected_state(graph)
    with pytest.raises(SnapshotPinsError, match="--pin-confirmed"):
        snapshot_pin(graph, older.name, ABSENT_REVISION, pin_confirmed=False)
    missing_confirm = _run(
        "pin",
        older.name,
        "--graph",
        str(graph),
        "--expected-registry-revision",
        ABSENT_REVISION,
        "--json",
    )
    assert missing_confirm.returncode == 2
    assert missing_confirm.stdout == ""
    assert "--pin-confirmed" in missing_confirm.stderr
    missing_rev = _run(
        "pin",
        older.name,
        "--graph",
        str(graph),
        "--pin-confirmed",
        "--json",
    )
    assert missing_rev.returncode == 2
    assert missing_rev.stdout == ""
    assert "expected-registry-revision" in missing_rev.stderr
    missing_unpin = _run(
        "unpin",
        older.name,
        "--graph",
        str(graph),
        "--expected-registry-revision",
        ABSENT_REVISION,
        "--json",
    )
    assert missing_unpin.returncode == 2
    assert missing_unpin.stdout == ""
    assert "--unpin-confirmed" in missing_unpin.stderr
    assert not (graph / OPERATOR_PINS_NAME).exists()
    assert _protected_state(graph) == before


def test_stale_cas_changes_nothing(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    first = snapshot_pin(graph, older.name, ABSENT_REVISION, pin_confirmed=True)
    before = _protected_state(graph)
    before_reg = (graph / OPERATOR_PINS_NAME).read_bytes()
    before_stat = (graph / OPERATOR_PINS_NAME).lstat()
    with pytest.raises(SnapshotPinsIntegrityError, match="does not match"):
        snapshot_pin(graph, newer.name, ABSENT_REVISION, pin_confirmed=True)
    stale = _run(
        "unpin",
        older.name,
        "--graph",
        str(graph),
        "--expected-registry-revision",
        ABSENT_REVISION,
        "--unpin-confirmed",
        "--json",
    )
    assert stale.returncode == 1
    assert stale.stdout == ""
    assert (graph / OPERATOR_PINS_NAME).read_bytes() == before_reg
    after_stat = (graph / OPERATOR_PINS_NAME).lstat()
    assert (
        after_stat.st_mtime_ns,
        after_stat.st_size,
        stat.S_IMODE(after_stat.st_mode),
        after_stat.st_ino,
    ) == (
        before_stat.st_mtime_ns,
        before_stat.st_size,
        stat.S_IMODE(before_stat.st_mode),
        before_stat.st_ino,
    )
    assert _protected_state(graph) == before
    assert first["operator_pins"] == [older.name]


def test_target_validation_rejects_unsafe_and_missing_ids(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    staging = graph / "snapshots" / f"{STAGING_NAME_PREFIX}{older.name}"
    staging.mkdir()
    (staging / "entities.parquet").write_bytes(b"partial")
    hashes_before = {
        key: value
        for key, value in _payload_hashes(graph).items()
        if key != OPERATOR_PINS_NAME
    }
    cases = [
        "current",
        f"{STAGING_NAME_PREFIX}{older.name}",
        "../escape",
        "a/b",
        "a\\b",
        f" {older.name}",
        f"{older.name} ",
        "",
        "not-a-real-snapshot-id",
    ]
    for value in cases:
        proc = _run(
            "pin",
            value,
            "--graph",
            str(graph),
            "--expected-registry-revision",
            ABSENT_REVISION,
            "--pin-confirmed",
            "--json",
        )
        assert proc.returncode == 2, (value, proc.stderr)
        assert proc.stdout == ""
    linked = tmp_path / "outside-snap"
    linked.mkdir()
    alias = graph / "snapshots" / "20260101-000000-alias01"
    alias.symlink_to(linked)
    linked_pin = _run(
        "pin",
        alias.name,
        "--graph",
        str(graph),
        "--expected-registry-revision",
        ABSENT_REVISION,
        "--pin-confirmed",
        "--json",
    )
    assert linked_pin.returncode == 2
    assert "symlink" in linked_pin.stderr.lower()
    assert not (graph / OPERATOR_PINS_NAME).exists()
    assert _current(graph) == newer.name
    assert {
        key: value
        for key, value in _payload_hashes(graph).items()
        if key != OPERATOR_PINS_NAME
    } == hashes_before


def test_malformed_registry_fails_closed(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    _publish(graph, "newer")
    path = graph / OPERATOR_PINS_NAME

    def reject(raw: bytes | str, match: str) -> None:
        if isinstance(raw, str):
            path.write_text(raw, encoding="utf-8")
        else:
            path.write_bytes(raw)
        before = path.read_bytes()
        with pytest.raises(SnapshotPinsError, match=match):
            snapshot_pins_list(graph)
        assert path.read_bytes() == before

    reject('{"schema_version":1,"schema_version":1,"pins":[]}\n', "duplicate")
    reject('{"schema_version":1,"pins":[],"extra":true}\n', "keys must be exactly")
    reject('{"schema_version":2,"pins":[]}\n', "schema_version")
    reject('{"schema_version":1.0,"pins":[]}\n', "schema_version")
    reject(
        json.dumps({"schema_version": 1, "pins": [older.name, older.name]}) + "\n",
        "unique and sorted",
    )
    later = "zzzzzzzz-000000-ffffffff"
    reject(
        json.dumps({"schema_version": 1, "pins": [later, older.name]}) + "\n",
        "unique and sorted",
    )
    reject(
        json.dumps({"schema_version": 1, "pins": ["current"]}) + "\n",
        "explicit published",
    )
    path.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(SnapshotPinsError, match="exceeds"):
        snapshot_pins_list(graph)

    path.unlink()
    path.symlink_to(tmp_path / "outside.json")
    (tmp_path / "outside.json").write_text(canonical_registry_text([older.name]))
    with pytest.raises(SnapshotPinsError, match="symlink"):
        snapshot_pins_list(graph)
    path.unlink()
    path.mkdir()
    with pytest.raises(SnapshotPinsError, match="not a regular file"):
        snapshot_pins_list(graph)


def test_only_registry_changes_on_successful_mutation(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    _publish(graph, "newer")
    before_hashes = _payload_hashes(graph)
    before_stats = _payload_stats(graph)
    snapshot_pin(graph, older.name, ABSENT_REVISION, pin_confirmed=True)
    after_hashes = _payload_hashes(graph)
    after_stats = _payload_stats(graph)
    assert OPERATOR_PINS_NAME in after_hashes
    assert OPERATOR_PINS_NAME not in before_hashes
    assert {key: value for key, value in after_hashes.items() if key != OPERATOR_PINS_NAME} == before_hashes
    assert {key: value for key, value in after_stats.items() if key != OPERATOR_PINS_NAME} == before_stats
    assert after_hashes["current"] == before_hashes["current"]


def test_pin_survives_keep_last_and_unpin_is_deferred(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "first")
    second = _publish(graph, "second")
    snapshot_pin(graph, first.name, ABSENT_REVISION, pin_confirmed=True)
    third = _publish(graph, "third", keep_last=1)
    survivors = {path.name for path in (graph / "snapshots").iterdir() if path.is_dir()}
    assert first.name in survivors
    assert third.name in survivors
    assert second.name not in survivors
    assert _current(graph) == third.name

    listed = snapshot_pins_list(graph)
    removed = snapshot_unpin(
        graph, first.name, listed["registry_revision"], unpin_confirmed=True
    )
    assert removed["immediate_deletion"] is False
    assert first.is_dir()
    assert _current(graph) == third.name

    fourth = _publish(graph, "fourth", keep_last=1)
    later = {path.name for path in (graph / "snapshots").iterdir() if path.is_dir()}
    assert first.name not in later
    assert fourth.name in later
    assert _current(graph) == fourth.name


def test_doc_claim_pins_remain_protected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import byog_graph

    graph = tmp_path / "g"
    first = _publish(graph, "first")
    _publish(graph, "second")
    monkeypatch.setattr(byog_graph, "pinned_snapshot_ids", lambda _root: {first.name})
    listed = snapshot_pins_list(graph)
    assert first.name in listed["claim_pins"]
    assert first.name in listed["effective_pins"]
    assert first.name not in listed["operator_pins"]
    third = _publish(graph, "third", keep_last=1)
    survivors = {path.name for path in (graph / "snapshots").iterdir() if path.is_dir()}
    assert first.name in survivors
    assert third.name in survivors


def test_malformed_registry_blocks_publication_before_current_changes(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "first")
    (graph / OPERATOR_PINS_NAME).write_text("{not-json", encoding="utf-8")
    before_current = _current(graph)
    listing = tuple(sorted(path.name for path in (graph / "snapshots").iterdir()))
    with pytest.raises(ValueError, match="operator pin registry"):
        _publish(graph, "blocked", keep_last=1)
    assert _current(graph) == before_current == first.name
    assert tuple(sorted(path.name for path in (graph / "snapshots").iterdir())) == listing
    with pytest.raises(ValueError, match="operator pin registry"):
        cleanup_old_snapshots(graph, keep_last=1)
    assert _current(graph) == first.name
    assert first.is_dir()


def test_registry_corruption_after_current_skips_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.byog_graph as byog

    graph = tmp_path / "g"
    first = _publish(graph, "first")
    second = _publish(graph, "second")
    registry = graph / OPERATOR_PINS_NAME
    registry.write_text(canonical_registry_text([]), encoding="utf-8")
    original_write = byog._atomic_write_text

    def corrupt_after_current(text: str, final_path: Path) -> None:
        original_write(text, final_path)
        if final_path == graph / "current":
            registry.write_text("{not-json", encoding="utf-8")

    monkeypatch.setattr(byog, "_atomic_write_text", corrupt_after_current)
    third = _publish(graph, "third", keep_last=1)

    assert _current(graph) == third.name
    assert first.is_dir()
    assert second.is_dir()
    assert third.is_dir()
    assert registry.read_text(encoding="utf-8") == "{not-json"


def test_cli_serializes_writes_and_flushes_under_each_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from contextlib import contextmanager

    import graphrag_code.snapshot_pins as pins_mod

    graph = tmp_path / "g"
    older = _publish(graph, "older")
    _publish(graph, "newer")
    original_list_scope = pins_mod._snapshot_pins_list_scope
    original_mutation_scope = pins_mod._snapshot_mutation_scope
    original_json = pins_mod.result_to_json
    original_format = pins_mod.format_result
    state = {"active": False, "responses": 0, "flushes": 0}

    @contextmanager
    def tracked_list_scope(*args, **kwargs):
        with original_list_scope(*args, **kwargs) as result:
            state["active"] = True
            try:
                yield result
            finally:
                state["active"] = False

    @contextmanager
    def tracked_mutation_scope(*args, **kwargs):
        with original_mutation_scope(*args, **kwargs) as result:
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

    monkeypatch.setattr(pins_mod, "_snapshot_pins_list_scope", tracked_list_scope)
    monkeypatch.setattr(pins_mod, "_snapshot_mutation_scope", tracked_mutation_scope)
    monkeypatch.setattr(pins_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(pins_mod, "format_result", guarded_format)
    monkeypatch.setattr(pins_mod.sys, "stdout", GuardedStdout())

    assert pins_mod.main(["list", "--graph", str(graph), "--json"]) == 0
    assert pins_mod.main(
        [
            "pin",
            older.name,
            "--graph",
            str(graph),
            "--expected-registry-revision",
            ABSENT_REVISION,
            "--pin-confirmed",
            "--json",
        ]
    ) == 0
    assert pins_mod.main(
        [
            "unpin",
            older.name,
            "--graph",
            str(graph),
            "--expected-registry-revision",
            _revision_of(graph),
            "--unpin-confirmed",
        ]
    ) == 0
    assert state["active"] is False
    assert state["responses"] >= 3
    assert state["flushes"] == 3


def _pin_worker(graph: str, snapshot: str, revision: str, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.snapshot_pins import (
        SnapshotPinsError,
        snapshot_pin,
    )

    try:
        result = snapshot_pin(
            ChildPath(graph),
            snapshot,
            revision,
            pin_confirmed=True,
        )
        q.put(("ok", result["changed"], result["registry_revision"]))
    except SnapshotPinsError as error:
        q.put(("err", error.exit_code, str(error)))
    except Exception as exc:
        q.put(("exc", type(exc).__name__, str(exc)))


def test_concurrent_mutations_same_revision_at_most_one_writes(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    q = CTX.Queue()
    workers = [
        CTX.Process(target=_pin_worker, args=(str(graph), older.name, ABSENT_REVISION, q)),
        CTX.Process(target=_pin_worker, args=(str(graph), newer.name, ABSENT_REVISION, q)),
    ]
    try:
        for worker in workers:
            worker.start()
        results = [q.get(timeout=TIMEOUT) for _ in workers]
        for worker in workers:
            worker.join(timeout=TIMEOUT)
            assert not worker.is_alive()
    finally:
        _cleanup_processes(*workers)
    outcomes = [item[0] for item in results]
    assert outcomes.count("ok") == 1
    assert outcomes.count("err") == 1
    errors = [item for item in results if item[0] == "err"]
    assert errors[0][1] == 1
    listed = snapshot_pins_list(graph)
    assert len(listed["operator_pins"]) == 1
    assert listed["operator_pins"][0] in {older.name, newer.name}


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
    ents = pd.DataFrame(
        [{"id": f"ent:{marker}", "title": marker, "type": "function", "source_file": "x.py"}]
    )
    rels = pd.DataFrame(
        [{"id": f"rel:{marker}", "source": "x.py", "target": marker, "type": "contains"}]
    )
    tus = pd.DataFrame(
        [{"id": f"tu:{marker}", "title": "x.py", "source_file": "x.py", "entity_id": f"ent:{marker}"}]
    )
    snap = byog.publish_byog_snapshot(
        ents, rels, tus, ChildPath(graph), keep_last=keep_last
    )
    q.put(snap.name)


def _pin_waiter(graph: str, snapshot: str, revision: str, about, got, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    import graphrag_code.byog_graph as byog
    from graphrag_code.snapshot_pins import snapshot_pin

    orig = byog._acquire_exclusive_lock

    def wrapped(fd):
        about.set()
        backend = orig(fd)
        got.set()
        return backend

    byog._acquire_exclusive_lock = wrapped
    try:
        result = snapshot_pin(
            ChildPath(graph),
            snapshot,
            revision,
            pin_confirmed=True,
        )
        q.put(result["pinned_snapshot"])
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def test_publisher_and_pin_wait_for_each_other(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "old", keep_last=1)
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_exclusive_hold, args=(str(graph), held, resume, q))
    pub = CTX.Process(target=_publisher, args=(str(graph), "new", 1, about, got, q))
    try:
        holder.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        assert _current(graph) == first.name
        resume.set()
        pub.join(timeout=TIMEOUT)
        holder.join(timeout=TIMEOUT)
        assert not pub.is_alive() and not holder.is_alive()
        assert got.is_set()
        assert _current(graph) != first.name
    finally:
        _cleanup_processes(holder, pub, release=resume)

    pin_target = Path(graph / "snapshots" / _current(graph))
    assert pin_target.is_dir()
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
        assert snapshot_pins_list(graph)["operator_pins"] == [pin_target.name]
    finally:
        _cleanup_processes(holder, actor, release=resume)


def test_lock_replacement_while_waiting_fails_closed(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    _publish(graph, "newer")
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_exclusive_hold, args=(str(graph), held, resume, q))
    actor = CTX.Process(
        target=_pin_waiter,
        args=(str(graph), older.name, ABSENT_REVISION, about, got, q),
    )
    try:
        holder.start()
        assert held.wait(timeout=TIMEOUT)
        actor.start()
        assert about.wait(timeout=TIMEOUT)
        lock = graph / PUBLICATION_LOCK_NAME
        lock.unlink()
        lock.write_bytes(b"replacement-lock-domain")
        resume.set()
        actor.join(timeout=TIMEOUT)
        holder.join(timeout=TIMEOUT)
        assert not actor.is_alive() and not holder.is_alive()
        messages = [q.get(timeout=TIMEOUT), q.get(timeout=TIMEOUT)]
        assert any("publication lock changed" in message for message in messages)
        assert not (graph / OPERATOR_PINS_NAME).exists()
    finally:
        _cleanup_processes(holder, actor, release=resume)


def test_missing_lock_and_legacy_flat_create_nothing(tmp_path: Path):
    managed = tmp_path / "managed"
    first = _publish(managed, "a")
    (managed / PUBLICATION_LOCK_NAME).unlink()
    before = list(managed.iterdir())
    proc = _run(
        "pin",
        first.name,
        "--graph",
        str(managed),
        "--expected-registry-revision",
        ABSENT_REVISION,
        "--pin-confirmed",
        "--json",
    )
    assert proc.returncode == 2
    assert "adopt-publication-lock" in proc.stderr
    assert not (managed / PUBLICATION_LOCK_NAME).exists()
    assert not (managed / OPERATOR_PINS_NAME).exists()
    assert {path.name for path in managed.iterdir()} == {path.name for path in before}

    flat = tmp_path / "flat"
    flat.mkdir()
    pd.DataFrame([{"id": "e"}]).to_parquet(flat / "entities.parquet")
    pd.DataFrame([{"id": "r"}]).to_parquet(flat / "relationships.parquet")
    pd.DataFrame([{"id": "t"}]).to_parquet(flat / "text_units.parquet")
    flat_before = _payload_hashes(flat)
    flat_proc = _run(
        "list",
        "--graph",
        str(flat),
        "--json",
    )
    assert flat_proc.returncode == 2
    assert "legacy" in flat_proc.stderr
    assert _payload_hashes(flat) == flat_before
    assert not (flat / OPERATOR_PINS_NAME).exists()
    assert not (flat / PUBLICATION_LOCK_NAME).exists()


def test_cli_module_script_and_wheel_parity(tmp_path: Path, built_wheel_and_sdist):
    from conftest import install_wheel

    graph = tmp_path / "g"
    older = _publish(graph, "older")
    _publish(graph, "newer")
    args = [
        "pin",
        older.name,
        "--graph",
        str(graph),
        "--expected-registry-revision",
        ABSENT_REVISION,
        "--pin-confirmed",
        "--json",
    ]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_pins", *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    (graph / OPERATOR_PINS_NAME).unlink()
    script = subprocess.run(
        [sys.executable, str(PINS), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    (graph / OPERATOR_PINS_NAME).unlink()
    cli = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "snapshot-pin",
            older.name,
            "--graph",
            str(graph),
            "--expected-registry-revision",
            ABSENT_REVISION,
            "--pin-confirmed",
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    bodies = [json.loads(proc.stdout) for proc in (module, script, cli)]
    assert bodies[0]["pinned_snapshot"] == bodies[1]["pinned_snapshot"] == bodies[2]["pinned_snapshot"]
    assert bodies[0]["operator_pins"] == bodies[1]["operator_pins"] == bodies[2]["operator_pins"]
    listed = snapshot_pins_list(graph)
    list_args = ["--graph", str(graph), "--json"]
    list_module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_pins", *list_args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    list_script = _run(*list_args, cwd=tmp_path)
    list_cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-pins", *list_args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert list_module.returncode == list_script.returncode == list_cli.returncode == 0
    assert json.loads(list_module.stdout)["operator_pins"] == listed["operator_pins"]
    assert json.loads(list_script.stdout)["registry_revision"] == listed["registry_revision"]
    assert json.loads(list_cli.stdout)["effective_pins"] == listed["effective_pins"]
    assert result_to_json(json.loads(list_module.stdout)) == list_module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    (graph / OPERATOR_PINS_NAME).unlink()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-pin",
            older.name,
            "--graph",
            str(graph),
            "--expected-registry-revision",
            ABSENT_REVISION,
            "--pin-confirmed",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    body = json.loads(installed.stdout)
    assert body["ok"] is True
    assert body["pinned_snapshot"] == older.name


def test_mcp_tool_set_remains_exactly_eleven(tmp_path: Path):
    from anyio import run as anyio_run

    graph = tmp_path / "g"
    _publish(graph, "a")
    assert len(TOOL_NAMES) == 11
    assert "snapshot_pin" not in TOOL_NAMES
    assert "snapshot_unpin" not in TOOL_NAMES
    assert "snapshot_pins" not in TOOL_NAMES
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 11
            assert "snapshot_pin" not in names

    anyio_run(_body)


def test_implementation_does_not_invoke_producers():
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
    assert "cleanup_old_snapshots" not in source
    assert "snapshot_activate" not in source
    assert "index_python" not in source
    assert "extract_c" not in source
    assert "_publication_lock" not in source
    assert "graph_read_lease" in source
    assert "graph_exclusive_lease" in source
