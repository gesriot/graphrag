#!/usr/bin/env python
"""Generic C package indexer for BYOG (Phase 6 bootstrap).

Walks a C package (.c/.h), runs the tree-sitter-c extractor, and publishes a
snapshot via the shared BYOG writer -- so audit_call_edges / context_pack /
port_eval work the same as for Python graphs.

Preprocessor liveness defaults to **no_compiler** (host-independent). Pass
``--compiler-builtins`` for local host-specific analysis; the snapshot manifest
then records the toolchain identity and macro-seed digest.

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
from c_preprocessor import (  # type: ignore
    ToolchainDriftError,
    annotate_byog,
    build_liveness_provenance,
    check_liveness_stamp_freshness,
)

app = typer.Typer(help="Generic C -> BYOG indexer (tree-sitter-c, Phase 6 bootstrap).")

SETTINGS = "workflows:\n  - create_communities\n  - create_community_reports\n"


@app.command()
def main(
    package: Path = typer.Option(..., "--package", "-p", exists=True, file_okay=False, dir_okay=True),
    graph: Path = typer.Option(..., "--graph", "-g"),
    keep_snapshots: int = typer.Option(5, "--keep-snapshots", "--keep-last"),
    compiler_builtins: bool = typer.Option(
        False,
        "--compiler-builtins/--no-compiler-builtins",
        help=(
            "Seed liveness from the host toolchain (compiler -E -dM). "
            "Default is off: published labels are host-independent (no_compiler). "
            "When on, the snapshot manifest records compiler id + macro-seed digest."
        ),
    ),
    allow_toolchain_drift: bool = typer.Option(
        False,
        "--allow-toolchain-drift",
        help=(
            "Permit re-stamping when a prior compiler_builtins stamp's macro-seed "
            "digest does not match this host. Default refuses silent relabels."
        ),
    ),
) -> None:
    pkg_dir = package.resolve()
    graph_dir = graph.resolve()

    # Refuse silent relabel when a prior stamp recorded a different seed.
    if graph_dir.exists() and (graph_dir / "current").exists():
        freshness = check_liveness_stamp_freshness(
            graph_dir,
            pkg_dir,
            use_compiler_builtins=compiler_builtins,
        )
        if (
            not allow_toolchain_drift
            and freshness["status"] == "drift"
            and (freshness.get("prior") or {}).get("macro_seed_digest")
        ):
            typer.secho(freshness["message"], fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        if freshness["status"] == "drift":
            typer.secho(
                f"note: liveness provenance differs from prior stamp "
                f"({freshness['message']})",
                fg=typer.colors.YELLOW,
                err=True,
            )
        elif freshness["status"] == "missing":
            typer.secho(
                "note: prior snapshot has no preprocessor_liveness; "
                "this publish will record eval_mode + macro_seed_digest",
                fg=typer.colors.YELLOW,
                err=True,
            )

    data = build_c_byog(pkg_dir)
    # Re-annotate with explicit publish policy (extract_c also annotates with
    # no_compiler by default; this makes the mode + digest authoritative).
    try:
        summary = annotate_byog(
            data,
            pkg_dir,
            use_compiler_builtins=compiler_builtins,
            graph_dir=graph_dir if (graph_dir / "current").exists() else None,
            allow_toolchain_drift=allow_toolchain_drift,
        )
    except ToolchainDriftError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e

    ents_df = pd.DataFrame(data["entities"])
    rels_df = pd.DataFrame(data["relationships"])
    tus_df = pd.DataFrame(data["text_units"])
    obs_df = pd.DataFrame(data.get("call_observations", []))
    print(f"Indexing {pkg_dir} -> {graph_dir}")
    print(f"  Entities: {len(ents_df)}, Relationships: {len(rels_df)}, TextUnits: {len(tus_df)}")
    if len(obs_df):
        print(f"  Call observations: {len(obs_df)}")
    n_pp_ent = int(ents_df["preprocessor_dependent"].fillna(False).astype(bool).sum()) if "preprocessor_dependent" in ents_df.columns else 0
    n_pp_call = 0
    if "preprocessor_dependent" in rels_df.columns and "type" in rels_df.columns:
        calls = rels_df[rels_df["type"].astype(str) == "calls"]
        n_pp_call = int(calls["preprocessor_dependent"].fillna(False).astype(bool).sum())
    print(f"  Preprocessor-dependent (provenance): entities={n_pp_ent}, calls={n_pp_call}")
    print(
        f"  Liveness eval_mode={summary.get('eval_mode')} "
        f"digest={summary.get('liveness_provenance', {}).get('macro_seed_digest')} "
        f"host_independent={summary.get('liveness_provenance', {}).get('host_independent')}"
    )
    extra = {"preprocessor_liveness": summary.get("liveness_provenance")}
    snap_dir = publish_byog_snapshot(
        ents_df, rels_df, tus_df, graph_dir, SETTINGS,
        keep_last=keep_snapshots, source_root=pkg_dir,
        call_observations_df=obs_df if len(obs_df) > 0 else None,
        extra_manifest=extra,
    )
    print(f"Done. Snapshot: {snap_dir}")


if __name__ == "__main__":
    app()
