"""A published graph must match what the current extractor produces.

A semantic extractor change applied to some published graphs and not others
leaves the rest silently stale. Both happened: the cross-module import resolver
(`e83eee8`) was published to four graphs and not the others, and the
registry-dispatch promotion was published only to `byog_jsonpatch`. Nothing
noticed, because every gate reindexes fresh into `output/port_gates/` and never
reads the published artifact — so gates stayed green while `byog_semver`,
`byog_mini_game`, `byog_dmp` and `byog_charset_normalizer` disagreed with the
code that generated them by 2 to 72 calls.

`byog_isodate` is exempt and must stay exempt: it is the frozen graph behind the
closed ablation experiment's adequacy claim, deliberately pinned to the older
extractor.

Run: uv run python -m pytest examples/mini_game/tests/test_graph_extractor_drift.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from mini_game_to_byog import build_byog_for_package  # type: ignore

# package directory -> published graph root
LIVE_PYTHON_GRAPHS = {
    "mini_game": "byog_mini_game",
    "mini_lang": "byog_mini_lang",
    "jsonpatch": "byog_jsonpatch",
    "sqlparse": "byog_sqlparse",
    "semantic_version": "byog_semver",
    "diff_match_patch": "byog_dmp",
    "humanize": "byog_humanize",
    "charset_normalizer": "byog_charset_normalizer",
}

# Frozen on purpose: backs `isodate_adequacy_v3` and the closed experiment.
FROZEN_GRAPHS = {"byog_isodate"}


def _published_calls(graph_root: Path) -> int:
    snapshot = (graph_root / "current").read_text().strip()
    rels = pd.read_parquet(graph_root / "snapshots" / snapshot / "relationships.parquet")
    return int((rels["type"].astype(str) == "calls").sum())


@pytest.mark.parametrize("package,graph_name", sorted(LIVE_PYTHON_GRAPHS.items()))
def test_published_graph_matches_current_extractor(package: str, graph_name: str):
    graph_root = ROOT / graph_name
    if not (graph_root / "current").is_file():
        pytest.skip(f"{graph_name} not published locally")
    fresh = build_byog_for_package(package_dir=ROOT / "examples" / package)
    fresh_calls = sum(1 for r in fresh["relationships"] if r.get("type") == "calls")
    published_calls = _published_calls(graph_root)
    assert fresh_calls == published_calls, (
        f"{graph_name} has {published_calls} calls but the current extractor "
        f"produces {fresh_calls}. Reindex it "
        f"(scripts/index_python.py --package examples/{package} --graph {graph_name}) "
        "so the published artifact matches the code that generates it."
    )


def test_frozen_graphs_are_excluded_deliberately():
    """The exemption list is explicit, and every entry is really frozen."""
    assert FROZEN_GRAPHS == {"byog_isodate"}
    assert not (FROZEN_GRAPHS & set(LIVE_PYTHON_GRAPHS.values()))
    import json

    manifest = json.loads((ROOT / "scripts" / "doc_claims.json").read_text())
    frozen_claim_graphs = {
        Path(str(c["source"].get("graph") or "")).name
        for c in manifest["claims"]
        if c.get("kind") == "frozen_snapshot"
    }
    assert "byog_isodate" in frozen_claim_graphs, frozen_claim_graphs
