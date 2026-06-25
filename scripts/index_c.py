#!/usr/bin/env python
"""Generic C package indexer for BYOG (Phase 6 bootstrap).

Walks a C package (.c/.h), runs the tree-sitter-c extractor, and publishes a
snapshot via the shared BYOG writer -- so audit_call_edges / context_pack /
port_eval work the same as for Python graphs.

Usage:
    uv run python scripts/index_c.py --package examples/jsmn --graph byog_jsmn
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import typer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from extract_c import build_c_byog  # type: ignore
from byog_graph import publish_byog_snapshot  # type: ignore

app = typer.Typer(help="Generic C -> BYOG indexer (tree-sitter-c, Phase 6 bootstrap).")

SETTINGS = "workflows:\n  - create_communities\n  - create_community_reports\n"


@app.command()
def main(
    package: Path = typer.Option(..., "--package", "-p", exists=True, file_okay=False, dir_okay=True),
    graph: Path = typer.Option(..., "--graph", "-g"),
    keep_snapshots: int = typer.Option(5, "--keep-snapshots", "--keep-last"),
) -> None:
    pkg_dir = package.resolve()
    data = build_c_byog(pkg_dir)
    ents_df = pd.DataFrame(data["entities"])
    rels_df = pd.DataFrame(data["relationships"])
    tus_df = pd.DataFrame(data["text_units"])
    obs_df = pd.DataFrame(data.get("call_observations", []))
    print(f"Indexing {pkg_dir} -> {graph.resolve()}")
    print(f"  Entities: {len(ents_df)}, Relationships: {len(rels_df)}, TextUnits: {len(tus_df)}")
    if len(obs_df):
        print(f"  Call observations: {len(obs_df)}")
    snap_dir = publish_byog_snapshot(
        ents_df, rels_df, tus_df, graph.resolve(), SETTINGS,
        keep_last=keep_snapshots, source_root=pkg_dir,
        call_observations_df=obs_df if len(obs_df) > 0 else None,
    )
    print(f"Done. Snapshot: {snap_dir}")


if __name__ == "__main__":
    app()
