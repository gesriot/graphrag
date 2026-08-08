#!/usr/bin/env python
"""Generic C package indexer for BYOG (Phase 6 bootstrap).

Walks a C package (.c/.h), runs the tree-sitter-c extractor, and publishes a
snapshot via the shared BYOG writer -- so audit_call_edges / context_pack /
port_eval work the same as for Python graphs.

Preprocessor liveness defaults to **no_compiler** (host-independent). Pass
``--compiler-builtins`` for local host-specific analysis; the snapshot manifest
then records the toolchain identity and macro-seed digest.

Optional ``--compiler-dependencies`` overlays flattened compiler-backed
translation-unit ``depends_on`` edges from ``compile_commands.json`` (default
off). Optional ``--compiler-includes`` overlays direct ``includes`` edges from
``compiler -E -H`` (also default off). Optional ``--clang-signatures`` attaches
configured Clang function-signature fields to matched tree-sitter function
entities (default off; diagnostic audit remains standalone). Published graph
counts stay tree-sitter-only unless an overlay is explicitly enabled.

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
from c_clang_signatures import (  # type: ignore
    ClangSignatureError,
    append_clang_signatures,
    build_disabled_provenance as build_disabled_signature_provenance,
)
from c_compiler_facts import (  # type: ignore
    CompilerDependencyError,
    append_compiler_dependencies,
    build_disabled_provenance as build_disabled_dependency_provenance,
)
from c_compiler_includes import (  # type: ignore
    CompilerIncludeError,
    append_compiler_includes,
    build_disabled_provenance as build_disabled_include_provenance,
)
from c_preprocessor import (  # type: ignore
    ToolchainDriftError,
    annotate_byog,
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
    compiler_dependencies: bool = typer.Option(
        False,
        "--compiler-dependencies/--no-compiler-dependencies",
        help=(
            "Overlay flattened compiler-backed translation-unit depends_on edges "
            "from compile_commands.json (compiler -M, then package-path filtering). "
            "Default is off so published "
            "tree-sitter-c graph counts stay unchanged. When on, missing "
            "compile_commands.json or a broken compiler fails explicitly."
        ),
    ),
    compiler_includes: bool = typer.Option(
        False,
        "--compiler-includes/--no-compiler-includes",
        help=(
            "Overlay direct includes edges from compile_commands.json via "
            "compiler -E -H (configured include hierarchy). Default is off. "
            "Independently selectable from --compiler-dependencies. When on, "
            "missing compile_commands.json or a broken compiler fails explicitly."
        ),
    ),
    clang_signatures: bool = typer.Option(
        False,
        "--clang-signatures/--no-clang-signatures",
        help=(
            "Attach configured Clang function-signature fields to existing "
            "tree-sitter function entities using the AST function-definition "
            "audit (matched + line-confirmed only). Default is off. Unclean "
            "audit residuals (clang_only/ambiguous/macro_location_unsupported) "
            "fail the overlay explicitly. Does not create entities or edges."
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

    # Optional compiler overlays (each default off; independently selectable).
    if compiler_dependencies:
        try:
            dep_prov = append_compiler_dependencies(data, pkg_dir)
        except CompilerDependencyError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from e
    else:
        dep_prov = build_disabled_dependency_provenance()

    if compiler_includes:
        try:
            inc_prov = append_compiler_includes(data, pkg_dir)
        except CompilerIncludeError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from e
    else:
        inc_prov = build_disabled_include_provenance()

    if clang_signatures:
        try:
            sig_prov = append_clang_signatures(data, pkg_dir)
        except ClangSignatureError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from e
    else:
        sig_prov = build_disabled_signature_provenance()

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
    if dep_prov.get("enabled"):
        print(
            f"  Compiler depends_on: facts={dep_prov.get('n_facts')} "
            f"TUs={dep_prov.get('n_translation_units')} "
            f"digest={dep_prov.get('compile_commands_digest')} "
            f"compiler={dep_prov.get('compiler_id')}"
        )
    else:
        print("  Compiler depends_on: off (default tree-sitter-c graph)")
    if inc_prov.get("enabled"):
        print(
            f"  Compiler includes: facts={inc_prov.get('n_facts')} "
            f"TUs={inc_prov.get('n_translation_units')} "
            f"digest={inc_prov.get('compile_commands_digest')} "
            f"compiler={inc_prov.get('compiler_id')}"
        )
    else:
        print("  Compiler includes: off (default tree-sitter-c graph)")
    if sig_prov.get("enabled"):
        counts = sig_prov.get("counts") or {}
        print(
            f"  Clang signatures: facts={sig_prov.get('n_facts')} "
            f"matched={counts.get('matched')} "
            f"tree_sitter_only={counts.get('tree_sitter_only')} "
            f"out_of_scope={counts.get('out_of_compile_db_scope')} "
            f"digest={sig_prov.get('compile_commands_digest')} "
            f"compiler={sig_prov.get('compiler_id')}"
        )
    else:
        print("  Clang signatures: off (default tree-sitter-c graph)")
    extra = {
        "preprocessor_liveness": summary.get("liveness_provenance"),
        "compiler_dependencies": dep_prov,
        "compiler_includes": inc_prov,
        "clang_signatures": sig_prov,
    }
    snap_dir = publish_byog_snapshot(
        ents_df, rels_df, tus_df, graph_dir, SETTINGS,
        keep_last=keep_snapshots, source_root=pkg_dir,
        call_observations_df=obs_df if len(obs_df) > 0 else None,
        extra_manifest=extra,
    )
    print(f"Done. Snapshot: {snap_dir}")


if __name__ == "__main__":
    app()
