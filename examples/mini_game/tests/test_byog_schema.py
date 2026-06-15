"""Schema validation tests for GraphRAG BYOG outputs.

Ensures:
- Required columns present
- No dangling edges (relationship source/target resolve to entity titles)
- weight, provenance (source_file, extractor, confidence, is_deterministic) present
- human_readable_id etc.

Run after generating bridges/smoke:
    PYTHONPATH=. uv run python -m pytest examples/mini_game/tests/test_byog_schema.py -q
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

BYOG_ROOTS = [
    Path(__file__).parents[3] / "byog_smoke",       # the hand-fixed smoke
    Path(__file__).parents[3] / "byog_mini_game",   # the bridge output
]


def _load_parquets(root: Path):
    out = root / "output"
    ents = pd.read_parquet(out / "entities.parquet")
    rels = pd.read_parquet(out / "relationships.parquet")
    tus = pd.read_parquet(out / "text_units.parquet") if (out / "text_units.parquet").exists() else pd.DataFrame()
    return ents, rels, tus


@pytest.mark.parametrize("root", BYOG_ROOTS)
def test_byog_required_columns_and_no_dangling(root):
    if not (root / "output" / "entities.parquet").exists():
        pytest.skip(f"No BYOG output at {root}")

    ents, rels, tus = _load_parquets(root)

    # Required for BYOG + communities (per GraphRAG + our extensions)
    for col in ("id", "title", "description", "text_unit_ids"):
        assert col in ents.columns, f"entities missing {col}"

    for col in ("id", "source", "target", "description", "weight", "text_unit_ids"):
        assert col in rels.columns, f"relationships missing {col}"

    for col in ("id", "human_readable_id", "text", "n_tokens", "document_id", "entity_ids", "relationship_ids", "covariate_ids"):
        assert col in tus.columns, f"text_units missing {col}"

    # Provenance we require
    for col in ("source_file", "extractor", "confidence", "is_deterministic"):
        assert col in ents.columns
        assert col in rels.columns

    # weight present and sensible
    assert (rels["weight"] > 0).all()

    # No dangling: every rel source/target must appear in entity titles (the canonical for matching)
    entity_titles = set(ents["title"].astype(str))
    for _, r in rels.iterrows():
        assert str(r["source"]) in entity_titles, f"dangling source {r['source']} not in titles"
        assert str(r["target"]) in entity_titles, f"dangling target {r['target']} not in titles"

    # No dangling text-unit references from entities/relationships.
    text_unit_ids = set(tus["id"].astype(str))
    for frame_name, frame in (("entities", ents), ("relationships", rels)):
        for _, row in frame.iterrows():
            for text_unit_id in row["text_unit_ids"]:
                assert str(text_unit_id) in text_unit_ids, (
                    f"{frame_name} row {row['id']} references missing text unit {text_unit_id}"
                )

    # human_readable_id if used
    if "human_readable_id" in ents.columns:
        assert ents["human_readable_id"].notna().all()


def test_byog_smoke_specific_alignment():
    """Explicit check on the smoke that we fixed source/target to titles."""
    root = Path(__file__).parents[3] / "byog_smoke"
    if not (root / "output" / "entities.parquet").exists():
        pytest.skip("smoke not generated")
    ents, rels, _ = _load_parquets(root)
    titles = set(ents["title"].astype(str))
    for _, r in rels.iterrows():
        assert r["source"] in titles
        assert r["target"] in titles
        # In our fix we used short titles "update", "helper"
        assert r["source"] in {"update", "helper"} or "update" in str(r["source"])
