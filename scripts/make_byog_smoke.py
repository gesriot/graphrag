#!/usr/bin/env python
"""
BYOG smoke test generator.

Creates the three canonical GraphRAG Bring-Your-Own-Graph parquet tables
for a tiny "code" example (two functions in one module with a call edge).

This is the primary contract for Phase 0/1 MVP: deterministic parser output
must be serializable to these tables, then GraphRAG community/report workflows
can be run on top.

Run:
    uv run python scripts/make_byog_smoke.py
Then (when LLM configured):
    uv run graphrag index --root byog_smoke
    uv run graphrag query --root byog_smoke --method global "What are the main components?"
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def _atomic_write_parquet(df: pd.DataFrame, final_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=final_path.parent, suffix=".parquet.tmp", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        table = pa.Table.from_pandas(df)
        pq.write_table(table, tmp_path)
        os.replace(tmp_path, final_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _atomic_write_text(text: str, final_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=final_path.parent, suffix=".tmp", delete=False, mode="w", encoding="utf-8"
    ) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    try:
        os.replace(tmp_path, final_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def build_smoke_byog() -> dict:
    """Return the BYOG data dict (entities, relationships, text_units) for the smoke example.
    Used by CLI and by self-contained tests (no disk side effects).
    """
    # --- text_units (source "chunks" the graph was "extracted" from) ---
    text_units = pd.DataFrame([
        {
            "id": "tu:mod1",
            "human_readable_id": 1,
            "text": "module: mini_example.py\n\nContains update() and helper().",
            "n_tokens": 12,
            "document_id": "doc:mini_example",
            "document_ids": ["doc:mini_example"],
            "entity_ids": ["ent:file:mini_example", "ent:fn:update", "ent:fn:helper"],
            "relationship_ids": ["rel:update_calls_helper"],
            "covariate_ids": [],
        },
        {
            "id": "tu:fn_update",
            "human_readable_id": 2,
            "text": "def update(state):\n    ... calls helper()",
            "n_tokens": 8,
            "document_id": "doc:mini_example",
            "document_ids": ["doc:mini_example"],
            "entity_ids": ["ent:fn:update"],
            "relationship_ids": ["rel:update_calls_helper"],
            "covariate_ids": [],
        },
    ])

    # --- entities (nodes) ---
    entities = pd.DataFrame([
        {
            "id": "ent:file:mini_example",
            "title": "mini_example.py",
            "type": "file",
            "description": "Top-level module containing simulation helpers.",
            "text_unit_ids": ["tu:mod1"],
            "human_readable_id": 1,
            "source_file": "examples/mini_game/sim.py",
            "span": "1:0-50:0",
            "extractor": "manual-smoke",
            "confidence": 1.0,
            "is_deterministic": True,
            "document_ids": ["doc:mini_example"],
            "covariate_ids": [],
        },
        {
            "id": "ent:fn:update",
            "title": "update",
            "type": "function",
            "description": "Main simulation step. Mutates state and may call helper.",
            "text_unit_ids": ["tu:fn_update"],
            "human_readable_id": 2,
            "source_file": "examples/mini_game/sim.py",
            "span": "def update",
            "extractor": "manual-smoke",
            "confidence": 1.0,
            "is_deterministic": True,
            "document_ids": ["doc:mini_example"],
            "covariate_ids": [],
        },
        {
            "id": "ent:fn:helper",
            "title": "helper",
            "type": "function",
            "description": "Low-level physics helper called by update.",
            "text_unit_ids": ["tu:mod1"],
            "human_readable_id": 3,
            "source_file": "examples/mini_game/physics.py",
            "span": "def helper",
            "extractor": "manual-smoke",
            "confidence": 0.9,
            "is_deterministic": False,
            "document_ids": ["doc:mini_example"],
            "covariate_ids": [],
        },
    ])

    # --- relationships (edges) ---
    relationships = pd.DataFrame([
        {
            "id": "rel:update_calls_helper",
            "source": "update",  # canonical title (must match entity.title for GraphRAG 3.1 create_communities)
            "target": "helper",
            "type": "calls",
            "description": "update() invokes helper() on every physics step for collision checks.",
            "weight": 1.0,
            "text_unit_ids": ["tu:fn_update", "tu:mod1"],
            "human_readable_id": 1,
            "source_file": "examples/mini_game/sim.py",
            "span": "update: calls helper",
            "extractor": "manual-smoke",
            "confidence": 1.0,
            "is_deterministic": True,
            "document_ids": ["doc:mini_example"],
            "covariate_ids": [],
        },
    ])

    return {
        "entities": entities,
        "relationships": relationships,
        "text_units": text_units,
    }


def main() -> None:
    """CLI entrypoint that writes to the conventional location (for backward compat and manual use)."""
    OUT_DIR = Path("byog_smoke/output")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    data = build_smoke_byog()
    entities = data["entities"]
    relationships = data["relationships"]
    text_units = data["text_units"]

    _atomic_write_parquet(text_units, OUT_DIR / "text_units.parquet")
    _atomic_write_parquet(entities, OUT_DIR / "entities.parquet")
    _atomic_write_parquet(relationships, OUT_DIR / "relationships.parquet")

    print("Wrote BYOG tables to", OUT_DIR)
    print("entities:", len(entities), "relationships:", len(relationships))

    SETTINGS = """\
# Minimal BYOG settings for code graph experiments.
# See https://microsoft.github.io/graphrag/index/byog/
input:
  type: file
  file_type: text
  base_dir: "input"
output:
  base_dir: "output"
llm:
  model: "gpt-4.1"
  api_base: "https://api.openai.com/v1"
  api_key: ${OPENAI_API_KEY}
embeddings:
  model: "text-embedding-3-small"
workflows:
  - create_communities
  - create_community_reports
"""
    _atomic_write_text(SETTINGS, OUT_DIR.parent / "settings.yaml")
    print("Wrote settings.yaml")

    provenance = {
        "note": "This smoke graph was hand-authored to exercise the BYOG path. Real implementation must emit the same columns from tree-sitter + semantic analyzers with full provenance.",
        "required_columns": {
            "entities": ["id", "title", "description", "text_unit_ids"],
            "relationships": ["id", "source", "target", "description", "weight", "text_unit_ids"],
        },
        "code_extensions": ["source_file", "span", "extractor", "confidence", "is_deterministic"],
    }
    _atomic_write_text(json.dumps(provenance, indent=2), OUT_DIR / "provenance_smoke.json")
    print("Done.")


if __name__ == "__main__":
    main()
