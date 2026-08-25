"""Read-only snapshot retention plan.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_retention_plan.py -q
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
    plan_snapshot_retention,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import TOOL_NAMES, build_mcp_server, build_session  # type: ignore
from graphrag_code.snapshot_pins import (  # type: ignore
    ABSENT_REVISION,
    OPERATOR_PINS_NAME,
    canonical_registry_text,
    snapshot_pin,
)
from graphrag_code.snapshot_retention import (  # type: ignore
    SnapshotRetentionError,
    SnapshotRetentionIntegrityError,
    canonical_plan_revision_text,
    format_result,
    plan_revision_of,
    result_to_json,
    snapshot_retention_plan,
)

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
SCRIPT = ROOT / "scripts" / "snapshot_retention.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_retention.py"
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
        settings_text=f"retention-plan: {marker}\n",
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


def _assert_id_arrays(plan: dict) -> None:
    byte_sorted = {
        "operator_pins",
        "claim_pins",
        "effective_pins",
        "existing_operator_pins",
        "existing_claim_pins",
        "dangling_operator_pins",
        "dangling_claim_pins",
        "published_snapshots",
        "retained_snapshots",
        "deletion_candidates",
    }
    for key in byte_sorted:
        values = plan[key]
        assert values == sorted(values, key=lambda item: item.encode("utf-8"))
        assert values == list(dict.fromkeys(values))


def test_empty_single_and_multiple_snapshot_plans(tmp_path: Path):
    graph = tmp_path / "g"
    only = _publish(graph, "only")
    before = _protected_state(graph)
    single = snapshot_retention_plan(graph, 1)
    assert single["schema_version"] == 1
    assert single["graph"] == str(graph.resolve())
    assert single["keep_last_requested"] == 1
    assert single["keep_last_effective"] == 1
    assert single["current"] == only.name
    assert single["registry_revision"] == ABSENT_REVISION
    assert single["published_count"] == 1
    assert single["published_snapshots"] == [only.name]
    assert single["retained_snapshots"] == [only.name]
    assert single["deletion_candidates"] == []
    assert single["operator_pins"] == []
    assert single["staging_notices"] == []
    assert single["plan_revision"].startswith("sha256:")
    _assert_id_arrays(single)
    assert _protected_state(graph) == before

    older = only
    mid = _publish(graph, "mid")
    newest = _publish(graph, "newest")
    multi = snapshot_retention_plan(graph, 2)
    published = sorted(
        [older.name, mid.name, newest.name], key=lambda item: item.encode("utf-8")
    )
    remaining = [sid for sid in published if sid != newest.name]
    assert multi["keep_last_effective"] == 2
    assert multi["current"] == newest.name
    assert multi["published_count"] == 3
    assert multi["published_snapshots"] == published
    assert multi["retained_snapshots"] == sorted(
        [newest.name, remaining[-1]], key=lambda item: item.encode("utf-8")
    )
    assert multi["deletion_candidates"] == remaining[:-1]
    human = format_result(multi)
    assert "deleted" not in human.lower()
    assert "prun" not in human.lower()
    assert newest.name in human
    assert "plan_revision=" in human
    assert not (graph / OPERATOR_PINS_NAME).exists()
    with pytest.raises(ValueError, match="current snapshot id is required"):
        plan_snapshot_retention(
            keep_last=1,
            current_id=None,
            published_ids=[only.name],
        )
    with pytest.raises(ValueError, match="pin id is not a published id"):
        plan_snapshot_retention(
            keep_last=1,
            current_id=only.name,
            published_ids=[only.name],
            claim_pins=["../unsafe"],
        )


def test_current_and_newest_keep_last_semantics(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    third = _publish(graph, "c")
    fourth = _publish(graph, "d")
    (graph / "current").write_text(second.name + "\n", encoding="utf-8")
    published = sorted(
        [first.name, second.name, third.name, fourth.name],
        key=lambda item: item.encode("utf-8"),
    )
    plan = snapshot_retention_plan(graph, 2)
    remaining = [sid for sid in published if sid != second.name]
    assert plan["current"] == second.name
    assert plan["published_snapshots"] == published
    assert plan["retained_snapshots"] == sorted(
        [second.name, remaining[-1]], key=lambda item: item.encode("utf-8")
    )
    assert plan["deletion_candidates"] == remaining[:-1]
    zero = snapshot_retention_plan(graph, 0)
    assert zero["keep_last_requested"] == 0
    assert zero["keep_last_effective"] == 1
    assert zero["retained_snapshots"] == [second.name]
    assert zero["deletion_candidates"] == remaining


def test_operator_and_claim_pins_survive_keep_last_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import byog_graph

    graph = tmp_path / "g"
    oldest = _publish(graph, "oldest")
    mid = _publish(graph, "mid")
    newest = _publish(graph, "newest")
    snapshot_pin(graph, oldest.name, ABSENT_REVISION, pin_confirmed=True)
    monkeypatch.setattr(byog_graph, "pinned_snapshot_ids", lambda _root: {mid.name})
    plan = snapshot_retention_plan(graph, 1)
    assert plan["current"] == newest.name
    assert plan["operator_pins"] == [oldest.name]
    assert plan["claim_pins"] == [mid.name]
    assert plan["effective_pins"] == sorted(
        [oldest.name, mid.name], key=lambda item: item.encode("utf-8")
    )
    assert plan["existing_operator_pins"] == [oldest.name]
    assert plan["existing_claim_pins"] == [mid.name]
    assert plan["dangling_operator_pins"] == []
    assert plan["dangling_claim_pins"] == []
    assert set(plan["retained_snapshots"]) == {oldest.name, mid.name, newest.name}
    assert plan["deletion_candidates"] == []
    assert plan["keep_last_effective"] == 1


def test_protected_pins_may_exceed_keep_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import byog_graph

    graph = tmp_path / "g"
    pins = [_publish(graph, f"p{i}") for i in range(3)]
    extra = _publish(graph, "extra")
    current = _publish(graph, "now")
    for snap in pins:
        listed = snapshot_retention_plan(graph, 5)
        snapshot_pin(
            graph,
            snap.name,
            listed["registry_revision"],
            pin_confirmed=True,
        )
    monkeypatch.setattr(
        byog_graph, "pinned_snapshot_ids", lambda _root: {pins[0].name}
    )
    plan = snapshot_retention_plan(graph, 1)
    assert plan["keep_last_effective"] == 1
    assert extra.name in plan["deletion_candidates"]
    assert set(plan["retained_snapshots"]) == {pins[0].name, pins[1].name, pins[2].name, current.name}
    assert extra.name not in plan["retained_snapshots"]


def test_dangling_operator_and_claim_pins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import byog_graph

    graph = tmp_path / "g"
    live = _publish(graph, "live")
    missing = "20240101-000000-deadbeef"
    (graph / OPERATOR_PINS_NAME).write_text(
        canonical_registry_text(
            sorted([live.name, missing], key=lambda item: item.encode("utf-8"))
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        byog_graph,
        "pinned_snapshot_ids",
        lambda _root: {"20240202-000000-claimmiss", live.name},
    )
    plan = snapshot_retention_plan(graph, 1)
    assert plan["existing_operator_pins"] == [live.name]
    assert plan["dangling_operator_pins"] == [missing]
    assert plan["existing_claim_pins"] == [live.name]
    assert plan["dangling_claim_pins"] == ["20240202-000000-claimmiss"]
    assert plan["retained_snapshots"] == [live.name]
    assert missing not in plan["retained_snapshots"]
    assert missing not in plan["published_snapshots"]
    assert "20240202-000000-claimmiss" not in plan["retained_snapshots"]


def test_staging_notices_are_excluded_from_candidates(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    staging = graph / "snapshots" / f"{STAGING_NAME_PREFIX}{first.name}"
    staging.mkdir()
    (staging / "manifest.json").write_text("{}", encoding="utf-8")
    plan = snapshot_retention_plan(graph, 1)
    assert first.name in plan["deletion_candidates"]
    assert second.name in plan["retained_snapshots"]
    assert staging.name not in plan["published_snapshots"]
    assert staging.name not in plan["deletion_candidates"]
    assert staging.name not in plan["retained_snapshots"]
    assert plan["staging_notices"]
    assert plan["staging_notices"][0]["n_staging"] == 1
    assert staging.name in plan["staging_notices"][0]["names"]
    assert staging.is_dir()

    unexpected = graph / "snapshots" / "published-looking-file"
    unexpected.write_text("not a snapshot directory", encoding="utf-8")
    with pytest.raises(SnapshotRetentionError, match="unexpected unsafe"):
        snapshot_retention_plan(graph, 1)
    with pytest.raises(ValueError, match="unexpected unsafe"):
        cleanup_old_snapshots(graph, keep_last=1)
    assert first.is_dir() and second.is_dir() and staging.is_dir()
    before_current = _current(graph)
    before_entries = sorted(path.name for path in (graph / "snapshots").iterdir())
    with pytest.raises(ValueError, match="unexpected unsafe"):
        _publish(graph, "blocked", keep_last=1)
    assert _current(graph) == before_current
    assert sorted(path.name for path in (graph / "snapshots").iterdir()) == before_entries


def test_absent_registry_is_not_created(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    before = _protected_state(graph)
    plan = snapshot_retention_plan(graph, 1)
    assert plan["registry_revision"] == ABSENT_REVISION
    assert plan["operator_pins"] == []
    assert not (graph / OPERATOR_PINS_NAME).exists()
    assert _protected_state(graph) == before


def test_malformed_and_unsafe_registry_fails_closed(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    before = _protected_state(graph)
    registry = graph / OPERATOR_PINS_NAME

    registry.write_text('{"schema_version": 1, "pins": [], "pins": []}\n', encoding="utf-8")
    proc = _run("--graph", str(graph), "--keep-last", "1", "--json")
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "malformed" in proc.stderr or "duplicate" in proc.stderr

    registry.write_text(
        '{"schema_version": 1, "pins": [], "extra": true}\n', encoding="utf-8"
    )
    assert _run("--graph", str(graph), "--keep-last", "1", "--json").returncode == 2

    registry.write_text('{"schema_version": 2, "pins": []}\n', encoding="utf-8")
    assert _run("--graph", str(graph), "--keep-last", "1", "--json").returncode == 2

    registry.write_text(
        '{"schema_version": 1, "pins": ["zzzzzzzz-000000-ffffffff"]}\n',
        encoding="utf-8",
    )
    # valid published-id format but file is not sorted uniquely with a second
    # later id; single entry is sorted. Use two unsorted instead:
    registry.write_text(
        canonical_registry_text(["zzzzzzzz-000000-ffffffff", "aaaaaaaa-000000-ffffffff"])
        .replace(
            '"aaaaaaaa-000000-ffffffff",\n    "zzzzzzzz-000000-ffffffff"',
            '"zzzzzzzz-000000-ffffffff",\n    "aaaaaaaa-000000-ffffffff"',
        ),
        encoding="utf-8",
    )
    assert _run("--graph", str(graph), "--keep-last", "1", "--json").returncode == 2

    registry.write_bytes(b"x" * (64 * 1024 + 1))
    assert _run("--graph", str(graph), "--keep-last", "1", "--json").returncode == 2

    registry.unlink()
    registry.symlink_to(tmp_path / "outside.json")
    (tmp_path / "outside.json").write_text(
        canonical_registry_text([]), encoding="utf-8"
    )
    linked = _run("--graph", str(graph), "--keep-last", "1", "--json")
    assert linked.returncode == 2
    assert "symlink" in linked.stderr.lower()
    registry.unlink()
    registry.mkdir()
    assert _run("--graph", str(graph), "--keep-last", "1", "--json").returncode == 2
    registry.rmdir()
    assert not (graph / OPERATOR_PINS_NAME).exists()
    assert _protected_state(graph) == before


def test_missing_replaced_symlinked_and_nonregular_lock(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    before_names = {path.name for path in graph.iterdir()}
    missing = _run("--graph", str(graph), "--keep-last", "1", "--json")
    assert missing.returncode == 2
    assert "adopt-publication-lock" in missing.stderr
    assert not (graph / PUBLICATION_LOCK_NAME).exists()
    assert not (graph / OPERATOR_PINS_NAME).exists()
    assert {path.name for path in graph.iterdir()} == before_names

    _publish(graph, "b")
    lock.unlink()
    target = tmp_path / "external.lock"
    target.write_text("untouched", encoding="utf-8")
    lock.symlink_to(target)
    linked = _run("--graph", str(graph), "--keep-last", "1", "--json")
    assert linked.returncode == 2
    assert "symlink" in linked.stderr.lower()
    assert target.read_text(encoding="utf-8") == "untouched"

    lock.unlink()
    lock.mkdir()
    nonreg = _run("--graph", str(graph), "--keep-last", "1", "--json")
    assert nonreg.returncode == 2
    assert lock.is_dir()
    assert not (graph / OPERATOR_PINS_NAME).exists()


def test_legacy_flat_layout_creates_nothing(tmp_path: Path):
    flat = tmp_path / "flat"
    flat.mkdir()
    pd.DataFrame([{"id": "e"}]).to_parquet(flat / "entities.parquet")
    pd.DataFrame([{"id": "r"}]).to_parquet(flat / "relationships.parquet")
    pd.DataFrame([{"id": "t"}]).to_parquet(flat / "text_units.parquet")
    before = _payload_hashes(flat)
    proc = _run("--graph", str(flat), "--keep-last", "1", "--json")
    assert proc.returncode == 2
    assert "legacy" in proc.stderr
    assert _payload_hashes(flat) == before
    assert not (flat / OPERATOR_PINS_NAME).exists()
    assert not (flat / PUBLICATION_LOCK_NAME).exists()


def test_relative_cwd_module_script_product_and_wheel_parity(
    tmp_path: Path, built_wheel_and_sdist
):
    from conftest import install_wheel

    here = tmp_path / "here"
    here.mkdir()
    graph = here / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    args = ["--graph", "g", "--keep-last", "1", "--json"]
    module = subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_retention", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = _run(*args, cwd=here)
    cli = subprocess.run(
        [sys.executable, str(CLI), "snapshot-retention-plan", *args],
        cwd=here,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    bodies = [json.loads(proc.stdout) for proc in (module, script, cli)]
    assert bodies[0] == bodies[1] == bodies[2]
    listed = snapshot_retention_plan(graph, 1)
    assert bodies[0]["plan_revision"] == listed["plan_revision"]
    assert bodies[0]["deletion_candidates"] == listed["deletion_candidates"]
    assert result_to_json(bodies[0]) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-retention-plan",
            "--graph",
            str(graph),
            "--keep-last",
            "1",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    assert json.loads(installed.stdout)["plan_revision"] == listed["plan_revision"]


def test_plan_revision_binds_decision_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import byog_graph

    graph = tmp_path / "g"
    older = _publish(graph, "older")
    newer = _publish(graph, "newer")
    base = snapshot_retention_plan(graph, 1)
    same = snapshot_retention_plan(graph, 1)
    assert base["plan_revision"] == same["plan_revision"]
    assert base["plan_revision"] == plan_revision_of(base)
    expected = (
        "sha256:"
        + hashlib.sha256(canonical_plan_revision_text(base).encode("utf-8")).hexdigest()
    )
    assert base["plan_revision"] == expected
    assert "plan_revision" not in canonical_plan_revision_text(base)
    revision_payload = json.loads(canonical_plan_revision_text(base))
    assert "graph" not in revision_payload
    assert revision_payload["schema_version"] == 1
    assert revision_payload["retained_snapshots"] == base["retained_snapshots"]
    assert revision_payload["deletion_candidates"] == base["deletion_candidates"]
    changed_decision = dict(base)
    changed_decision["deletion_candidates"] = []
    assert plan_revision_of(changed_decision) != base["plan_revision"]

    import graphrag_code.snapshot_retention as plan_mod

    original_discovery = plan_mod._published_and_notices

    def switch_current_during_discovery(root: Path):
        result = original_discovery(root)
        (root / "current").write_text(older.name, encoding="utf-8")
        return result

    monkeypatch.setattr(
        plan_mod, "_published_and_notices", switch_current_during_discovery
    )
    with pytest.raises(
        SnapshotRetentionIntegrityError, match="decision inputs changed"
    ):
        snapshot_retention_plan(graph, 1)
    (graph / "current").write_text(newer.name, encoding="utf-8")
    monkeypatch.setattr(plan_mod, "_published_and_notices", original_discovery)

    zero = snapshot_retention_plan(graph, 0)
    one = snapshot_retention_plan(graph, 1)
    assert zero["keep_last_effective"] == one["keep_last_effective"] == 1
    assert zero["plan_revision"] == one["plan_revision"]

    two = snapshot_retention_plan(graph, 2)
    assert two["plan_revision"] != base["plan_revision"]

    snapshot_pin(graph, older.name, ABSENT_REVISION, pin_confirmed=True)
    pinned = snapshot_retention_plan(graph, 1)
    assert pinned["plan_revision"] != base["plan_revision"]

    (graph / "current").write_text(older.name + "\n", encoding="utf-8")
    switched = snapshot_retention_plan(graph, 1)
    assert switched["current"] == older.name
    assert switched["plan_revision"] != pinned["plan_revision"]

    _publish(graph, "third")
    more = snapshot_retention_plan(graph, 1)
    assert more["published_count"] == 3
    assert more["plan_revision"] != switched["plan_revision"]

    monkeypatch.setattr(byog_graph, "pinned_snapshot_ids", lambda _root: {newer.name})
    claimed = snapshot_retention_plan(graph, 1)
    assert claimed["plan_revision"] != more["plan_revision"]


def test_shared_plan_matches_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import byog_graph

    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    third = _publish(graph, "c")
    fourth = _publish(graph, "d")
    snapshot_pin(graph, first.name, ABSENT_REVISION, pin_confirmed=True)
    monkeypatch.setattr(byog_graph, "pinned_snapshot_ids", lambda _root: {second.name})
    plan = snapshot_retention_plan(graph, 1)
    helper = plan_snapshot_retention(
        keep_last=1,
        current_id=plan["current"],
        published_ids=plan["published_snapshots"],
        operator_pins=plan["operator_pins"],
        claim_pins=plan["claim_pins"],
    )
    assert helper["retained_snapshots"] == plan["retained_snapshots"]
    assert helper["deletion_candidates"] == plan["deletion_candidates"]
    deleted = cleanup_old_snapshots(graph, keep_last=1)
    remaining = sorted(
        path.name
        for path in (graph / "snapshots").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    assert remaining == plan["retained_snapshots"]
    assert deleted == len(plan["deletion_candidates"])
    assert first.is_dir() and second.is_dir() and fourth.is_dir()
    assert not third.exists()
    assert _current(graph) == fourth.name

    broken = tmp_path / "broken"
    broken_first = _publish(broken, "first")
    broken_second = _publish(broken, "second")
    (broken / "current").write_text("not/a/published-id", encoding="utf-8")
    with pytest.raises(ValueError, match="current snapshot id"):
        cleanup_old_snapshots(broken, keep_last=1)
    assert broken_first.is_dir() and broken_second.is_dir()

    ambiguous = tmp_path / "ambiguous"
    ambiguous_first = _publish(ambiguous, "first")
    ambiguous_second = _publish(ambiguous, "second")
    original_write = byog_graph._atomic_write_text

    def switch_after_current(text: str, final_path: Path) -> None:
        original_write(text, final_path)
        if final_path == ambiguous / "current":
            original_write(ambiguous_first.name, final_path)

    monkeypatch.setattr(byog_graph, "_atomic_write_text", switch_after_current)
    ambiguous_third = _publish(ambiguous, "third", keep_last=1)
    assert _current(ambiguous) == ambiguous_first.name
    assert ambiguous_first.is_dir()
    assert ambiguous_second.is_dir()
    assert ambiguous_third.is_dir()


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
    assert "cleanup_old_snapshots" not in source
    assert "snapshot_activate" not in source
    assert "index_python" not in source
    assert "extract_c" not in source
    assert "_publication_lock" not in source
    assert "graph_exclusive_lease" not in source
    assert "graph_read_lease" in source
    assert "plan_snapshot_retention" in source
    assert "load_operator_pins_unlocked" in imported


def test_cli_serializes_writes_and_flushes_under_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from contextlib import contextmanager

    import graphrag_code.snapshot_retention as plan_mod

    graph = tmp_path / "g"
    _publish(graph, "a")
    original_scope = plan_mod._snapshot_retention_plan_scope
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

    monkeypatch.setattr(plan_mod, "_snapshot_retention_plan_scope", tracked_scope)
    monkeypatch.setattr(plan_mod, "result_to_json", guarded_json)
    monkeypatch.setattr(plan_mod, "format_result", guarded_format)
    monkeypatch.setattr(plan_mod.sys, "stdout", GuardedStdout())
    assert plan_mod.main(["--graph", str(graph), "--keep-last", "1", "--json"]) == 0
    assert plan_mod.main(["--graph", str(graph), "--keep-last", "2"]) == 0
    assert state["active"] is False
    assert state["responses"] >= 2
    assert state["flushes"] == 2


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


def _plan_hold(graph: str, keep_last: int, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from pathlib import Path as ChildPath

    from graphrag_code.snapshot_retention import _snapshot_retention_plan_scope

    try:
        with _snapshot_retention_plan_scope(ChildPath(graph), keep_last) as result:
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                return
            q.put(("ok", result["plan_revision"]))
    except Exception as exc:
        q.put(f"error:{type(exc).__name__}:{exc}")


def _plan_waiter(graph: str, keep_last: int, about, got, q) -> None:
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
        from graphrag_code.snapshot_retention import snapshot_retention_plan

        result = snapshot_retention_plan(ChildPath(graph), keep_last)
        q.put(("ok", result["plan_revision"]))
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


def test_publisher_and_exclusive_wait_for_plan(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "old")
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    planner = CTX.Process(target=_plan_hold, args=(str(graph), 1, held, resume, q))
    pub = CTX.Process(target=_publisher, args=(str(graph), "new", 1, about, got, q))
    try:
        planner.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        assert _current(graph) == first.name
        resume.set()
        pub.join(timeout=TIMEOUT)
        planner.join(timeout=TIMEOUT)
        assert not pub.is_alive() and not planner.is_alive()
        assert got.is_set()
        assert _current(graph) != first.name
    finally:
        _cleanup_processes(planner, pub, release=resume)

    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_exclusive_hold, args=(str(graph), held, resume, q))
    waiter = CTX.Process(target=_plan_waiter, args=(str(graph), 1, about, got, q))
    try:
        holder.start()
        assert held.wait(timeout=TIMEOUT)
        waiter.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        resume.set()
        waiter.join(timeout=TIMEOUT)
        holder.join(timeout=TIMEOUT)
        assert not waiter.is_alive() and not holder.is_alive()
        assert got.is_set()
        outcomes = [q.get(timeout=TIMEOUT), q.get(timeout=TIMEOUT)]
        assert any(item == "held" or (isinstance(item, tuple) and item[0] == "ok") for item in outcomes)
    finally:
        _cleanup_processes(holder, waiter, release=resume)


def test_lock_replacement_while_plan_waits_fails_closed(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    before = _protected_state(graph)
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    holder = CTX.Process(target=_exclusive_hold, args=(str(graph), held, resume, q))
    waiter = CTX.Process(target=_plan_waiter, args=(str(graph), 1, about, got, q))
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
        assert not (graph / OPERATOR_PINS_NAME).exists()
        assert _current(graph) == before["current"]
        assert tuple(
            sorted(path.name for path in (graph / "snapshots").iterdir())
        ) == before["listing"]
    finally:
        _cleanup_processes(holder, waiter, release=resume)


def test_mcp_tool_set_remains_exactly_eleven(tmp_path: Path):
    from anyio import run as anyio_run

    graph = tmp_path / "g"
    _publish(graph, "a")
    assert len(TOOL_NAMES) == 12
    assert "snapshot_retention_plan" not in TOOL_NAMES
    assert "snapshot_retention" not in TOOL_NAMES
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        from mcp import Client

        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert names == set(TOOL_NAMES)
            assert len(names) == 12
            assert "snapshot_retention_plan" not in names

    anyio_run(_body)


def test_successful_and_failed_plans_leave_payload_unchanged(tmp_path: Path):
    graph = tmp_path / "g"
    older = _publish(graph, "older")
    _publish(graph, "newer")
    snapshot_pin(graph, older.name, ABSENT_REVISION, pin_confirmed=True)
    before = _protected_state(graph)
    ok = snapshot_retention_plan(graph, 1)
    assert older.name in ok["retained_snapshots"]
    assert _protected_state(graph) == before

    bad = _run("--graph", str(graph), "--json")
    assert bad.returncode == 2
    assert bad.stdout == ""
    assert _protected_state(graph) == before

    with pytest.raises(SnapshotRetentionError):
        snapshot_retention_plan(tmp_path / "missing", 1)
    assert _protected_state(graph) == before
    assert "Traceback" not in bad.stderr
