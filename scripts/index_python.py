#!/usr/bin/env python
"""
Generic Python package indexer for BYOG (Phase 1+).

Walks any Python package directory (non-test .py files), runs the tree-sitter + AST
extractor (with optional advanced Jedi/Pyright), performs two-pass title resolution,
preserves call observations (guarded / ambiguous / builtin-container / annotation),
and publishes a full snapshot under the target graph root (with atomic writes,
snapshots/<id>/ + current pointer, call_observations.parquet, etc.).

This is the generic replacement for the mini_game-specific mini_game_to_byog.py.

Usage:
    uv run python scripts/index_python.py \
        --package examples/mini_game \
        --graph byog_mini_game \
        --keep-snapshots 5 \
        --use-advanced

After indexing you can use:
    uv run python scripts/graph_query.py observations sim:run_simulation --graph byog_xxx
    uv run python scripts/context_pack.py sim:run_simulation --graph byog_xxx ...
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import pandas as pd
import typer

# Make local imports work whether run as module or script
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from mini_game_to_byog import build_byog_for_package  # type: ignore
from byog_graph import publish_byog_snapshot  # type: ignore

app = typer.Typer(help="Generic Python -> BYOG indexer (replaces mini_game_to_byog for general use).")

SETTINGS = """\
input:
  type: file
  file_type: text
  base_dir: "input"
output:
  base_dir: "output"
llm:
  model: "gpt-4.1"
  api_key: ${OPENAI_API_KEY}
embeddings:
  model: "text-embedding-3-small"
workflows:
  - create_communities
  - create_community_reports
"""


@app.command()
def main(
    package: Path = typer.Option(
        ...,
        "--package",
        "-p",
        help="Root directory of the Python package to index (will rglob for .py files).",
        exists=True,
        file_okay=False,
        dir_okay=True,
    ),
    graph: Path = typer.Option(
        ...,
        "--graph",
        "-g",
        help="Target BYOG graph root (snapshot layout with current + snapshots/).",
    ),
    keep_snapshots: int = typer.Option(
        5,
        "--keep-snapshots",
        "--keep-last",
        help="Maximum number of snapshots to retain (current is always protected).",
    ),
    use_advanced: bool = typer.Option(
        False,
        "--use-advanced",
        "--use-jedi-pyright",
        help="Enable optional local Jedi/Pyright for richer resolution.",
    ),
) -> None:
    """Index a Python package into a BYOG graph with full provenance and observations."""
    pkg_dir = package.resolve()
    graph_root = graph.resolve()

    data: Dict[str, Any] = build_byog_for_package(
        use_advanced=use_advanced, package_dir=pkg_dir
    )

    ents_df = pd.DataFrame(data["entities"])
    rels_df = pd.DataFrame(data["relationships"])
    tus_df = pd.DataFrame(data["text_units"])
    obs_df = pd.DataFrame(data.get("call_observations", []))

    # Ensure required columns for BYOG compatibility
    for df in (ents_df, rels_df, tus_df):
        for col in ("document_ids", "covariate_ids"):
            if col not in df.columns:
                df[col] = [[] for _ in range(len(df))]

    print(f"Indexing {pkg_dir} -> {graph_root}")
    print(f"  Entities: {len(ents_df)}, Relationships: {len(rels_df)}, TextUnits: {len(tus_df)}")
    if len(obs_df):
        print(f"  Call observations (weak/ambiguous/container/etc.): {len(obs_df)}")

    snap_dir = publish_byog_snapshot(
        ents_df,
        rels_df,
        tus_df,
        graph_root,
        SETTINGS,
        keep_last=keep_snapshots,
        source_root=pkg_dir,
        call_observations_df=obs_df if len(obs_df) > 0 else None,
    )

    print(f"Done. Snapshot: {snap_dir}")
    print("Use graph_query / context_pack against the --graph root.")


if __name__ == "__main__":
    app()
