"""Retention must never delete a snapshot a doc claim pins.

`keep_last=5` cleanup destroyed the sqlparse phase-5 baseline
(`20260618-151436-ad7b5954`) on 2026-07-28: a cross-module resolver change
republished `byog_sqlparse` twice, the sixth-oldest snapshot fell off, and the
claim that verified it silently degraded to an "absent source" skip that read as
a pass. The numbers it backed can never be re-derived — the extractor has moved
on — so pinned snapshots are evidence, not cache.

Run: uv run python -m pytest examples/mini_game/tests/test_snapshot_retention.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from byog_graph import cleanup_old_snapshots, pinned_snapshot_ids  # type: ignore


def _make_graph(root: Path, snapshot_ids: list[str], current: str) -> None:
    (root / "snapshots").mkdir(parents=True)
    for sid in snapshot_ids:
        (root / "snapshots" / sid).mkdir()
        (root / "snapshots" / sid / "manifest.json").write_text("{}")
    (root / "current").write_text(current)


def test_pinned_ids_come_from_the_claims_manifest():
    pinned = pinned_snapshot_ids(ROOT / "byog_sqlparse")
    manifest = json.loads((ROOT / "scripts" / "doc_claims.json").read_text())
    expected = {
        c["source"]["snapshot"]
        for c in manifest["claims"]
        if isinstance(c.get("source", {}).get("snapshot"), str)
        and Path(str(c["source"].get("graph") or "")).name == "byog_sqlparse"
    }
    assert pinned == expected, (pinned, expected)
    assert pinned, "sqlparse must still pin at least one snapshot"


def test_pinned_snapshot_survives_retention(tmp_path: Path, monkeypatch):
    """A pinned id is kept even when it is older than keep_last would allow."""
    import byog_graph

    graph = tmp_path / "byog_sqlparse"
    ids = [f"2026070{n}-000000-aaaaaaa{n}" for n in range(1, 9)]
    pinned_id = ids[0]  # the oldest — exactly the case that was lost
    _make_graph(graph, ids, current=ids[-1])
    monkeypatch.setattr(byog_graph, "pinned_snapshot_ids", lambda _root: {pinned_id})

    byog_graph.cleanup_old_snapshots(graph, keep_last=3)
    survivors = {d.name for d in (graph / "snapshots").iterdir()}
    assert pinned_id in survivors, survivors
    assert ids[-1] in survivors, survivors
    # Unpinned old snapshots are still collected.
    assert ids[1] not in survivors, survivors


def test_unpinned_snapshots_are_still_collected(tmp_path: Path, monkeypatch):
    import byog_graph

    graph = tmp_path / "byog_other"
    ids = [f"2026070{n}-000000-bbbbbbb{n}" for n in range(1, 9)]
    _make_graph(graph, ids, current=ids[-1])
    monkeypatch.setattr(byog_graph, "pinned_snapshot_ids", lambda _root: set())

    deleted = byog_graph.cleanup_old_snapshots(graph, keep_last=3)
    survivors = {d.name for d in (graph / "snapshots").iterdir()}
    assert deleted == len(ids) - 3, (deleted, survivors)
    assert len(survivors) == 3


def test_every_pinned_snapshot_that_survives_is_on_disk():
    """A pinned id missing from disk means evidence was lost — say so loudly."""
    missing = []
    for graph in sorted(ROOT.glob("byog_*")):
        for sid in pinned_snapshot_ids(graph):
            if not (graph / "snapshots" / sid).is_dir():
                missing.append(f"{graph.name}/{sid}")
    assert not missing, (
        "doc claims pin snapshots that are not on disk; either restore them or "
        f"reclassify the claim as historical: {missing}"
    )
