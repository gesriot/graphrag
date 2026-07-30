"""Published mutable graphs must match their current extractor.

The extractor-drift incident was possible because source profiles rebuilt fresh
``output/port_gates`` graphs while the published local roots were never read.
This test exercises the same manifest-derived health check used by the full
portfolio gate.  It deliberately does not reindex or alter any ``byog_*`` root.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from byog_graph import publish_byog_snapshot  # type: ignore
from published_graph_health import (  # type: ignore
    PublishedGraphSpec,
    _fresh_data,
    check_spec,
    load_specs,
)


SPECS = load_specs()
MUTABLE_SPECS = [spec for spec in SPECS if spec.mode == "mutable"]
FROZEN_SPECS = [spec for spec in SPECS if spec.mode == "frozen"]


@pytest.mark.parametrize("spec", MUTABLE_SPECS, ids=lambda spec: spec.ident)
def test_published_graph_matches_current_extractor(spec: PublishedGraphSpec):
    result = check_spec(spec)
    # A clean checkout has no published local artifact, which is an explicit
    # health-stage skip.  A present stale/malformed root is never a skip.
    assert result["status"] in {"pass", "skipped"}, result


def test_frozen_graphs_are_declared_deliberately():
    """The single exemption is config-derived and anchored to the frozen claim."""
    assert [(spec.ident, spec.graph) for spec in FROZEN_SPECS] == [("isodate", "byog_isodate")]
    assert FROZEN_SPECS[0].reason
    # A frozen root is evidence of the closed experiment, not current extractor
    # output: health must not open, mutate, or attempt to replace it.
    assert check_spec(FROZEN_SPECS[0], graph_root=ROOT / "does-not-exist")["status"] == "exempt"
    manifest = json.loads((ROOT / "scripts" / "doc_claims.json").read_text())
    frozen_claim_graphs = {
        Path(str(claim["source"].get("graph") or "")).name
        for claim in manifest["claims"]
        if claim.get("kind") == "frozen_snapshot"
    }
    assert "byog_isodate" in frozen_claim_graphs, frozen_claim_graphs


def test_stale_current_snapshot_fails_health_check(tmp_path: Path):
    """A present graph pointing at stale content is a fail, not an artifact skip."""
    spec = next(candidate for candidate in MUTABLE_SPECS if candidate.ident == "mini_game")
    fresh = _fresh_data(spec, ROOT)
    stale_relationships = list(fresh["relationships"])
    stale_relationships.pop()
    graph = tmp_path / "byog_mini_game"
    publish_byog_snapshot(
        pd.DataFrame(fresh["entities"]),
        pd.DataFrame(stale_relationships),
        pd.DataFrame(fresh["text_units"]),
        graph,
        "health-test",
        source_root=(ROOT / spec.source).resolve(),
    )

    result = check_spec(spec, graph_root=graph)
    assert result["status"] == "fail", result
    assert result["reason"] == "published graph disagrees with current extractor"
    assert result["mismatches"]["relationships"]["missing_from_published"] == 1
