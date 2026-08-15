"""Bounded read-only snapshot history and structural snapshot diff.

Disposable tmp graphs only. Does not write published byog_* roots.

Run:
  uv run python -m pytest examples/mini_game/tests/test_snapshot_compare.py -q
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
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
    graph_read_lease,
    publish_byog_snapshot,
)
from graphrag_code.mcp_server import (  # type: ignore
    HARD_MAX_ENVELOPE_BYTES,
    GraphMcpError,
    build_mcp_server,
    build_session,
    preflight_graph,
)
from graphrag_code.snapshot_compare import (  # type: ignore
    HARD_MAX_DIFF_ITEMS,
    HARD_MAX_HISTORY_LIMIT,
    snapshot_diff,
    snapshot_history,
    result_to_json,
)
from mcp import Client

CTX = multiprocessing.get_context("spawn")
TIMEOUT = 60
COMPARE = ROOT / "scripts" / "snapshot_compare.py"
CLI = ROOT / "scripts" / "graphrag_code.py"
MODULE = ROOT / "src" / "graphrag_code" / "snapshot_compare.py"
FORBIDDEN = frozenset(
    {
        "index_python",
        "index_c",
        "extract_c",
        "extract_python",
        "publish_byog_snapshot",
        "cleanup_old_snapshots",
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


def _rows(marker: str, *, extra: dict | None = None, obs: bool = True):
    fields = extra or {}
    ents = [
        {
            "id": f"ent:{marker}",
            "title": f"demo:{marker}",
            "type": "function",
            "source_file": f"{marker}.py",
            "extractor": "tree-sitter-python",
            "description": f"desc-{marker}",
            **fields,
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
    observations = (
        [{"id": f"obs:{marker}", "caller": f"demo:{marker}", "callee": "len"}]
        if obs
        else None
    )
    return ents, rels, tus, observations


def _publish(graph: Path, marker: str, *, extra=None, obs: bool = True, keep_last: int = 10) -> Path:
    ents, rels, tus, observations = _rows(marker, extra=extra, obs=obs)
    return publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text=f"compare: {marker}\n",
        keep_last=keep_last,
        call_observations_df=pd.DataFrame(observations) if observations else None,
    )


def _current(graph: Path) -> str:
    return (graph / "current").read_text(encoding="utf-8").strip()


def _unlock(graph: Path) -> Path:
    lock = graph / PUBLICATION_LOCK_NAME
    if lock.exists() or lock.is_symlink():
        lock.unlink()
    return lock


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


def _run(graph: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "graphrag_code.snapshot_compare", *args, "--graph", str(graph)],
        cwd=str(graph.parent),
        capture_output=True,
        text=True,
        env=_child_env(),
    )


def test_history_order_and_current_marker(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a").name
    second = _publish(graph, "b").name
    third = _publish(graph, "c").name
    result = snapshot_history(graph, limit=20)
    ids = [entry["id"] for entry in result["snapshots"]]
    assert ids == sorted([first, second, third], reverse=True)
    assert result["current"] == third == _current(graph)
    by_id = {entry["id"]: entry for entry in result["snapshots"]}
    assert by_id[third]["is_current"] is True
    assert by_id[first]["is_current"] is False
    assert result["total"] == result["returned"] == 3
    assert result["truncated"] is False
    assert result["ok"] is True


def test_history_limit_truncation_keeps_exact_total(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import graphrag_code.snapshot_compare as compare

    graph = tmp_path / "g"
    ids = [_publish(graph, marker).name for marker in ("a", "b", "c")]
    original = compare._fingerprint
    fingerprinted: list[list[str]] = []

    def wrapped(graph_root, snap_dirs):
        fingerprinted.append([Path(path).name for path in snap_dirs])
        return original(graph_root, snap_dirs)

    monkeypatch.setattr(compare, "_fingerprint", wrapped)
    result = snapshot_history(graph, limit=2)
    expected = sorted(ids, reverse=True)
    assert [entry["id"] for entry in result["snapshots"]] == expected[:2]
    assert result["total"] == 3
    assert result["returned"] == 2
    assert result["truncated"] is True
    # A bounded history request hashes only the returned payloads (and current,
    # already among them here), while the snapshots-listing hash covers total
    # membership. Retained payloads outside the limit are not read.
    expected_fingerprints = expected[:2]
    if ids[-1] not in expected_fingerprints:
        expected_fingerprints.append(ids[-1])
    assert fingerprinted == [expected_fingerprints, expected_fingerprints]


def test_staging_is_notice_not_published_history(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "a")
    staging_names = [f"{STAGING_NAME_PREFIX}{number:02d}" for number in range(25)]
    for name in staging_names:
        staging = graph / "snapshots" / name
        staging.mkdir()
        (staging / "entities.parquet").write_bytes(b"not-a-snapshot")
    result = snapshot_history(graph)
    assert [entry["id"] for entry in result["snapshots"]] == [snap.name]
    notices = result["publication_notices"]
    assert any(notice["code"] == "staging_present" for notice in notices)
    staging_notice = next(
        notice for notice in notices if notice["code"] == "staging_present"
    )
    assert staging_notice["n_staging"] == 25
    assert staging_notice["names"] == staging_names[:20]
    assert staging_notice["returned"] == 20
    assert staging_notice["truncated"] is True
    assert all(not entry["id"].startswith(".") for entry in result["snapshots"])


def test_diff_added_removed_modified_all_tables(tmp_path: Path):
    graph = tmp_path / "g"
    before = _publish(graph, "old")
    ents = [
        {
            "id": "ent:old",
            "title": "demo:old",
            "type": "function",
            "source_file": "moved.py",
            "extractor": "tree-sitter-python",
            "description": "changed",
        },
        {
            "id": "ent:new",
            "title": "demo:new",
            "type": "function",
            "source_file": "new.py",
            "extractor": "tree-sitter-python",
            "description": "new",
        },
    ]
    rels = [
        {
            "id": "rel:new",
            "source": "demo:new.py",
            "target": "demo:new",
            "type": "contains",
            "extractor": "tree-sitter-python",
        }
    ]
    tus = [
        {
            "id": "tu:old",
            "title": "old.py",
            "source_file": "old.py",
            "entity_id": "ent:old",
        },
        {
            "id": "tu:new",
            "title": "new.py",
            "source_file": "new.py",
            "entity_id": "ent:new",
        },
    ]
    obs = [
        {"id": "obs:old", "caller": "demo:old", "callee": "print"},
        {"id": "obs:new", "caller": "demo:new", "callee": "len"},
    ]
    after = publish_byog_snapshot(
        pd.DataFrame(ents),
        pd.DataFrame(rels),
        pd.DataFrame(tus),
        graph,
        settings_text="compare: new\n",
        keep_last=10,
        call_observations_df=pd.DataFrame(obs),
        extra_manifest={"note": "after"},
    )
    result = snapshot_diff(graph, before.name, after.name, max_items=50)
    tables = result["tables"]
    assert tables["entities"]["added"]["items"] == ["ent:new"]
    assert tables["entities"]["removed"]["total"] == 0
    assert tables["entities"]["modified"]["items"] == [
        {"id": "ent:old", "changed_fields": ["description", "source_file"]}
    ]
    assert tables["relationships"]["added"]["items"] == ["rel:new"]
    assert tables["relationships"]["removed"]["items"] == ["rel:old"]
    assert tables["text_units"]["added"]["items"] == ["tu:new"]
    assert tables["call_observations"]["added"]["items"] == ["obs:new"]
    assert tables["call_observations"]["modified"]["items"] == [
        {"id": "obs:old", "changed_fields": ["callee"]}
    ]
    assert result["totals"]["added"] == 4
    assert result["from_snapshot"] == before.name
    assert result["to_snapshot"] == after.name
    assert result["identical"] is False
    assert "note" in result["manifest"]["added_keys"]


def test_null_and_nan_normalize_and_inf_fails(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a", extra={"score": None})
    second = _publish(graph, "a", extra={"score": float("nan")})
    same = snapshot_diff(graph, first.name, second.name)
    modified = same["tables"]["entities"]["modified"]
    # Both snapshots use id ent:a; explicit null and NaN canonicalize equally.
    assert modified["total"] == 0

    third = _publish(graph, "a", extra={"score": 1.0})
    changed = snapshot_diff(graph, second.name, third.name)
    fields = changed["tables"]["entities"]["modified"]["items"][0]["changed_fields"]
    assert fields == ["score"]

    from graphrag_code.snapshot_compare import _canonical, SnapshotCompareError

    assert _canonical(float("nan")) is None
    assert _canonical(None) is None
    assert _canonical(pd.NaT) is None
    with pytest.raises(SnapshotCompareError, match="non-finite"):
        _canonical(math.inf)
    with pytest.raises(SnapshotCompareError, match="non-finite"):
        _canonical(float("-inf"))

    broken = tmp_path / "inf"
    _publish(broken, "z")
    snap = broken / "snapshots" / _current(broken)
    ents = pd.read_parquet(snap / "entities.parquet")
    ents["score"] = [math.inf]
    ents.to_parquet(snap / "entities.parquet")
    with pytest.raises(Exception, match="non-finite|not ok"):
        snapshot_diff(broken, _current(broken), _current(broken))


def test_duplicate_missing_nonstring_ids_fail_closed(tmp_path: Path):
    graph = tmp_path / "dup"
    _publish(graph, "a")
    snap = graph / "snapshots" / _current(graph)
    ents = pd.read_parquet(snap / "entities.parquet")
    ents = pd.concat([ents, ents], ignore_index=True)
    ents.to_parquet(snap / "entities.parquet")
    manifest = json.loads((snap / "manifest.json").read_text())
    manifest["counts"]["entities"] = 2
    (snap / "manifest.json").write_text(json.dumps(manifest, indent=2))
    with pytest.raises(Exception, match="duplicate id|not ok"):
        snapshot_diff(graph, "current", "current")

    missing = tmp_path / "missing-id"
    _publish(missing, "a")
    snap = missing / "snapshots" / _current(missing)
    ents = pd.read_parquet(snap / "entities.parquet")
    ents["id"] = [None]
    ents.to_parquet(snap / "entities.parquet")
    with pytest.raises(Exception, match="nonempty string|not ok"):
        snapshot_diff(missing, "current", "current")

    numeric = tmp_path / "numeric-id"
    _publish(numeric, "a")
    snap = numeric / "snapshots" / _current(numeric)
    ents = pd.read_parquet(snap / "entities.parquet")
    ents["id"] = [1]
    ents.to_parquet(snap / "entities.parquet")
    with pytest.raises(Exception, match="nonempty string|not ok"):
        snapshot_diff(numeric, "current", "current")

    from graphrag_code.snapshot_compare import (
        SnapshotCompareError,
        _diff_table,
        _index_rows,
    )

    with pytest.raises(SnapshotCompareError, match="duplicate id"):
        _index_rows("entities", [{"id": "x"}, {"id": "x"}])
    with pytest.raises(SnapshotCompareError, match="nonempty string"):
        _index_rows("entities", [{"title": "no-id"}])
    with pytest.raises(SnapshotCompareError, match="nonempty string"):
        _index_rows("entities", [{"id": ""}])
    with pytest.raises(SnapshotCompareError, match="nonempty string"):
        _index_rows("entities", [{"id": 3}])
    with pytest.raises(SnapshotCompareError, match="non-string field"):
        _index_rows("entities", [{"id": "x", 1: "bad-column"}])

    structural = _diff_table(
        "entities",
        [{"id": "missing-v-null"}, {"id": "bool-v-int", "value": True}],
        [{"id": "missing-v-null", "value": None}, {"id": "bool-v-int", "value": 1}],
        max_items=10,
    )
    assert structural["modified"]["items"] == [
        {"id": "bool-v-int", "changed_fields": ["value"]},
        {"id": "missing-v-null", "changed_fields": ["value"]},
    ]


def test_same_snapshot_is_zero_diff(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "a")
    result = snapshot_diff(graph, snap.name, snap.name)
    assert result["identical"] is True
    assert result["byte_identical"] is True
    assert result["totals"] == {"added": 0, "removed": 0, "modified": 0}
    for table in result["tables"].values():
        assert table["added"]["total"] == table["removed"]["total"] == table["modified"]["total"] == 0
        assert table["added"]["items"] == table["removed"]["items"] == []
        assert table["modified"]["items"] == []
        assert table["added"]["truncated"] is False
    both_current = snapshot_diff(graph, "current", "current")
    assert both_current["from_snapshot"] == both_current["to_snapshot"] == snap.name
    assert both_current["identical"] is True


def test_current_resolves_exactly_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import graphrag_code.snapshot_compare as compare
    from graphrag_code.byog_snapshot_graph_audit import resolve_snapshot as original

    graph = tmp_path / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    calls: list[object] = []

    def wrapped(graph_root, snapshot=None):
        if snapshot is None:
            calls.append(None)
        return original(graph_root, snapshot)

    monkeypatch.setattr(compare, "resolve_snapshot", wrapped)
    snapshot_history(graph)
    history_calls = list(calls)
    calls.clear()
    snapshot_diff(graph, "current", "current")
    assert history_calls == [None]
    assert calls == [None]


def test_invalid_traversal_and_staging_refs_fail_closed(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    before = _payload_hashes(graph)
    for ref in ("../etc/passwd", "foo/bar", f"{STAGING_NAME_PREFIX}x", "", ".", ".."):
        proc = _run(graph, "diff", "--from", ref, "--to", "current")
        assert proc.returncode == 2, (ref, proc.stderr)
        assert "Traceback" not in proc.stderr
    assert _payload_hashes(graph) == before


def test_symlinked_snapshot_core_and_lock_fail_closed(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "a")
    target = tmp_path / "outside"
    target.mkdir()
    alias = graph / "snapshots" / "aliased"
    alias.symlink_to(snap, target_is_directory=True)
    proc = _run(graph, "history")
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    alias.unlink()

    core = snap / "entities.parquet"
    payload = core.read_bytes()
    external = tmp_path / "entities.parquet"
    external.write_bytes(payload)
    core.unlink()
    core.symlink_to(external)
    proc = _run(graph, "history")
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    core.unlink()
    core.write_bytes(payload)

    lock = graph / PUBLICATION_LOCK_NAME
    lock.unlink()
    external_lock = tmp_path / "external.lock"
    external_lock.write_text("keep", encoding="utf-8")
    lock.symlink_to(external_lock)
    proc = _run(graph, "history")
    assert proc.returncode == 2
    assert "symlink" in proc.stderr
    assert external_lock.read_text(encoding="utf-8") == "keep"


def test_strict_cli_missing_lock_does_not_mutate(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    lock = _unlock(graph)
    before_hashes = _payload_hashes(graph)
    before_stats = _payload_stats(graph)
    hist = _run(graph, "history", "--json")
    diff = _run(graph, "diff", "--from", "current", "--to", "current", "--json")
    assert hist.returncode == diff.returncode == 2
    assert "adopt-publication-lock" in hist.stderr
    assert "adopt-publication-lock" in diff.stderr
    assert "Traceback" not in hist.stderr
    assert not lock.exists()
    assert _payload_hashes(graph) == before_hashes
    assert _payload_stats(graph) == before_stats


def test_legacy_cli_compatibility_does_not_create_lock(tmp_path: Path):
    graph = tmp_path / "g"
    snap = _publish(graph, "a")
    lock = _unlock(graph)
    hist = _run(graph, "history", "--allow-unlocked-legacy", "--json")
    assert hist.returncode == 0, hist.stderr
    body = json.loads(hist.stdout)
    assert body["ok"] is True
    assert body["legacy_unlocked"] is True
    assert body["retention_guarantee"] is False
    assert any(notice["code"] == "legacy_unlocked" for notice in body["publication_notices"])
    assert not lock.exists()
    diff = _run(
        graph,
        "diff",
        "--from",
        snap.name,
        "--to",
        "current",
        "--allow-unlocked-legacy",
        "--json",
    )
    assert diff.returncode == 0, diff.stderr
    assert json.loads(diff.stdout)["retention_guarantee"] is False
    assert not lock.exists()


def test_mcp_missing_lock_rejection_unchanged(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    lock = _unlock(graph)
    before = _payload_hashes(graph)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphrag_code.mcp_server",
            "--graph",
            str(graph),
            "--indexer",
            "python",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert proc.returncode == 2
    assert "publication lock is missing" in proc.stderr
    assert proc.stdout == ""
    assert not lock.exists()
    assert _payload_hashes(graph) == before


def test_mcp_history_and_diff_after_publication(tmp_path: Path):
    from anyio import run as anyio_run

    graph = tmp_path / "g"
    first = _publish(graph, "a")
    second = _publish(graph, "b")
    for number in range(25):
        (graph / "snapshots" / f"{STAGING_NAME_PREFIX}mcp-{number:02d}").mkdir()
    session = build_session(graph, "python")
    server = build_mcp_server(session)

    async def _body():
        async with Client(server) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert "snapshot_history" in tools
            assert "snapshot_diff" in tools
            history = await client.call_tool("snapshot_history", {"limit": 10})
            payload = history.structured_content
            if set(payload) == {"result"}:
                payload = payload["result"]
            assert payload["ok"] is True
            assert payload["tool"] == "snapshot_history"
            assert payload["snapshot"] == second.name
            assert payload["data"]["current"] == second.name
            assert payload["total"] == 2
            assert payload["data"]["truncated"] is False
            assert payload["truncated"] is True
            diff = await client.call_tool(
                "snapshot_diff",
                {"from_snapshot": first.name, "to_snapshot": "current", "max_items": 10},
            )
            body = diff.structured_content
            if set(body) == {"result"}:
                body = body["result"]
            assert body["ok"] is True
            assert body["data"]["from_snapshot"] == first.name
            assert body["data"]["to_snapshot"] == second.name
            assert body["truncated"] is True
            extra = await client.call_tool(
                "snapshot_history",
                {"limit": 10, "graph": str(tmp_path / "other")},
            )
            assert extra.is_error is True
            redirect = await client.call_tool(
                "snapshot_diff",
                {
                    "from_snapshot": first.name,
                    "output": str(tmp_path / "out.json"),
                },
            )
            assert redirect.is_error is True

    anyio_run(_body)


def test_mcp_hard_limits_and_envelope_overflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    graph = tmp_path / "g"
    _publish(graph, "a")
    session = build_session(graph, "python")
    with pytest.raises(GraphMcpError, match="must be <= "):
        session.snapshot_history(HARD_MAX_HISTORY_LIMIT + 1)
    with pytest.raises(GraphMcpError, match="must be <= "):
        session.snapshot_diff("current", "current", HARD_MAX_DIFF_ITEMS + 1)
    proc = _run(graph, "history", "--limit", str(HARD_MAX_HISTORY_LIMIT + 1))
    assert proc.returncode == 2
    import graphrag_code.mcp_server as mcp_mod

    monkeypatch.setattr(mcp_mod, "HARD_MAX_ENVELOPE_BYTES", 64)
    with pytest.raises(GraphMcpError, match="response envelope exceeds hard limit"):
        session.snapshot_history(1)


def _diff_holder(graph: str, from_id: str, held, resume, q) -> None:
    sys.path.insert(0, str(ROOT / "src"))
    from contextlib import contextmanager
    from pathlib import Path as ChildPath

    import graphrag_code.snapshot_compare as compare

    orig = compare.graph_read_lease

    @contextmanager
    def gated(*args, **kwargs):
        with orig(*args, **kwargs):
            held.set()
            if not resume.wait(timeout=TIMEOUT):
                q.put("timeout")
                raise TimeoutError("diff holder timed out")
            yield

    compare.graph_read_lease = gated
    try:
        result = compare.snapshot_diff(ChildPath(graph), from_id, "current")
        q.put(result["from_snapshot"])
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
    snap = byog.publish_byog_snapshot(ents, rels, tus, ChildPath(graph), keep_last=keep_last)
    q.put(snap.name)


def test_held_diff_blocks_keep_last_publisher(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a", keep_last=1)
    held = CTX.Event()
    resume = CTX.Event()
    about = CTX.Event()
    got = CTX.Event()
    q = CTX.Queue()
    reader = CTX.Process(target=_diff_holder, args=(str(graph), first.name, held, resume, q))
    pub = CTX.Process(target=_publisher, args=(str(graph), "b", 1, about, got, q))
    try:
        reader.start()
        assert held.wait(timeout=TIMEOUT)
        pub.start()
        assert about.wait(timeout=TIMEOUT)
        assert not got.is_set()
        assert _current(graph) == first.name
        resume.set()
        pub.join(timeout=TIMEOUT)
        reader.join(timeout=TIMEOUT)
        assert not pub.is_alive() and not reader.is_alive()
        assert got.is_set()
    finally:
        _cleanup_processes(reader, pub, release=resume)


def test_manual_mutation_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import graphrag_code.snapshot_compare as compare

    graph = tmp_path / "g"
    _publish(graph, "a")
    _publish(graph, "b")
    orig = compare._fingerprint
    calls = {"n": 0}

    def wrapped(*args, **kwargs):
        if calls["n"] == 1:
            (graph / "current").write_text("mutated-by-lock-ignorer\n", encoding="utf-8")
        calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(compare, "_fingerprint", wrapped)
    with pytest.raises(Exception, match="changed during the read"):
        snapshot_history(graph)
    assert calls["n"] >= 2

    # Discovery happens before the baseline fingerprint so the implementation
    # knows which bounded payloads to hash. A lock-ignoring current change in
    # that narrow interval must not turn the preliminary id into a stale but
    # apparently verified response.
    before_graph = tmp_path / "before-baseline"
    older = _publish(before_graph, "older")
    _publish(before_graph, "newer")
    baseline_calls = {"n": 0}

    def before_wrapped(*args, **kwargs):
        if baseline_calls["n"] == 0:
            (before_graph / "current").write_text(older.name + "\n", encoding="utf-8")
        baseline_calls["n"] += 1
        return orig(*args, **kwargs)

    monkeypatch.setattr(compare, "_fingerprint", before_wrapped)
    with pytest.raises(Exception, match="discovery changed during the read"):
        snapshot_history(before_graph)


def test_cli_module_script_and_wheel_parity(tmp_path: Path, built_wheel_and_sdist):
    from conftest import install_wheel

    graph = tmp_path / "g"
    first = _publish(graph, "a")
    _publish(graph, "b")
    module = subprocess.run(
        [
            sys.executable,
            "-m",
            "graphrag_code.snapshot_compare",
            "history",
            "--graph",
            str(graph),
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    script = subprocess.run(
        [sys.executable, str(COMPARE), "history", "--graph", str(graph), "--json"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    cli = subprocess.run(
        [
            sys.executable,
            str(CLI),
            "snapshot-history",
            "--graph",
            str(graph),
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_child_env(),
    )
    assert module.returncode == script.returncode == cli.returncode == 0, module.stderr
    assert json.loads(module.stdout) == json.loads(script.stdout) == json.loads(cli.stdout)
    assert result_to_json(json.loads(module.stdout)) == module.stdout

    env = install_wheel(built_wheel_and_sdist[0], tmp_path / "site")
    outside = tmp_path / "outside"
    outside.mkdir()
    installed = subprocess.run(
        [
            "graphrag-code",
            "snapshot-diff",
            "--graph",
            str(graph),
            "--from",
            first.name,
            "--to",
            "current",
            "--json",
        ],
        cwd=outside,
        capture_output=True,
        text=True,
        env=env,
    )
    assert installed.returncode == 0, installed.stderr
    body = json.loads(installed.stdout)
    assert body["from_snapshot"] == first.name
    assert body["ok"] is True


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
    assert "index_python" not in source
    assert "extract_c" not in source


def test_history_and_diff_do_not_mutate_graph(tmp_path: Path):
    graph = tmp_path / "g"
    first = _publish(graph, "a")
    _publish(graph, "b")
    before_hashes = _payload_hashes(graph)
    before_stats = _payload_stats(graph)
    listing = tuple(sorted(p.name for p in (graph / "snapshots").iterdir()))
    snapshot_history(graph, limit=10)
    snapshot_diff(graph, first.name, "current", max_items=10)
    assert _payload_hashes(graph) == before_hashes
    assert _payload_stats(graph) == before_stats
    assert tuple(sorted(p.name for p in (graph / "snapshots").iterdir())) == listing


def test_unexpected_snapshots_entry_fails_closed(tmp_path: Path):
    graph = tmp_path / "g"
    _publish(graph, "a")
    (graph / "snapshots" / "README.txt").write_text("nope", encoding="utf-8")
    proc = _run(graph, "history")
    assert proc.returncode == 2
    assert "unexpected" in proc.stderr
    proc = _run(graph, "diff", "--from", "current", "--to", "current")
    assert proc.returncode == 2
    assert "unexpected" in proc.stderr
