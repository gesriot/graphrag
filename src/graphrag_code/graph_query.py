#!/usr/bin/env python
"""
Local graph queries over BYOG parquets (no external API).

Provides:
- callers(symbol)
- callees(symbol)
- types_used_by(symbol)   # outgoing uses_type targets
- type_users(symbol)      # incoming uses_type sources
- type_closure(symbol)    # bounded cycle-safe transitive uses_type
- subgraph(symbol)        # bounded cycle-safe multi-hop induced subgraph
- components()            # weakly connected components (structural grouping)
- strong_components()     # directed strongly connected components
- condensation()          # directed SCC condensation DAG
- shortest_path()         # directed structural shortest path
- degree_ranking()        # raw directed relationship-row degree ranking
- neighbors(symbol)
- dependency_order()      # deterministic containment order over contains rows
- impact(symbol)
- symbol(query)
- observations(symbol_or_module)   # weak/ambiguous/container resolver diagnostics

Designed to be used from agent loops, context-pack, or directly from the shell.

Example:
    uv run python scripts/graph_query.py callers sim:run_simulation --graph byog_mini_game
    uv run python scripts/graph_query.py types-used-by ini:ini_parse --graph byog_inih
    uv run python scripts/graph_query.py type-closure ini:ini_parse --direction dependencies --max-depth 2
    uv run python scripts/graph_query.py subgraph sim:run_simulation --graph byog_mini_game --direction both
    uv run python scripts/graph_query.py subgraph sim:run_simulation --graph byog_mini_game --dot
    uv run python scripts/graph_query.py components --graph byog_mini_game
    uv run python scripts/graph_query.py strong-components --graph byog_mini_game
    uv run python scripts/graph_query.py condensation --graph byog_mini_game
    uv run python scripts/graph_query.py condensation --graph byog_mini_game --dot
    uv run python scripts/graph_query.py shortest-path sim:run_simulation sim:update --graph byog_mini_game
    uv run python scripts/graph_query.py degree-ranking --graph byog_mini_game
    uv run python scripts/graph_query.py dependency-order --graph byog_mini_game
    uv run python scripts/graph_query.py observations sim:run_simulation --graph byog_mini_game
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Dict, Any, Optional, Sequence

import pandas as pd
import sys
import typer

from graphrag_code.byog_graph import (  # re-export for backward compat
    DEFAULT_COMPONENTS_MAX_COMPONENTS,
    DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT,
    DEFAULT_CONDENSATION_MAX_COMPONENTS,
    DEFAULT_CONDENSATION_MAX_EDGES,
    DEFAULT_CONDENSATION_MAX_NODES_PER_COMPONENT,
    DEFAULT_DEGREE_RANKING_MAX_NODES,
    DEFAULT_SHORTEST_PATH_MAX_DEPTH,
    DEFAULT_STRONG_COMPONENTS_MAX_COMPONENTS,
    DEFAULT_STRONG_COMPONENTS_MAX_NODES_PER_COMPONENT,
    DEFAULT_SUBGRAPH_MAX_DEPTH,
    DEFAULT_SUBGRAPH_MAX_EDGES,
    DEFAULT_SUBGRAPH_MAX_NODES,
    DEFAULT_TYPE_CLOSURE_MAX_DEPTH,
    DEFAULT_TYPE_CLOSURE_MAX_EDGES,
    DEFAULT_TYPE_CLOSURE_MAX_NODES,
    HARD_MAX_COMPONENTS,
    HARD_MAX_COMPONENT_NODES,
    HARD_MAX_CONDENSATION_COMPONENT_NODES,
    HARD_MAX_CONDENSATION_COMPONENTS,
    HARD_MAX_CONDENSATION_EDGES,
    HARD_MAX_DEGREE_RANKING_NODES,
    HARD_MAX_SHORTEST_PATH_DEPTH,
    HARD_MAX_STRONG_COMPONENTS,
    HARD_MAX_STRONG_COMPONENT_NODES,
    HARD_MAX_SUBGRAPH_DEPTH,
    HARD_MAX_SUBGRAPH_EDGES,
    HARD_MAX_SUBGRAPH_NODES,
    ByogGraph,
    compute_bounded_subgraph,
    compute_condensation_graph,
    compute_containment_dependency_order,
    compute_shortest_path,
    compute_structural_degree_ranking,
    compute_strongly_connected_components,
    compute_uses_type_closure,
    compute_weakly_connected_components,
    load_graph,
)
from graphrag_code.snapshot_read import (
    SnapshotReadError,
    retained_snapshot_read,
)
from graphrag_code.condensation_dot import dumps_condensation_dot
from graphrag_code.subgraph_dot import dumps_subgraph_dot

app = typer.Typer(help="Local BYOG graph queries (callers, callees, impact, etc.)")


def _graph_opt() -> Any:
    return typer.Option(Path("byog_mini_game"), "--graph")


def _snapshot_opt() -> Any:
    return typer.Option(
        None,
        "--snapshot",
        help=(
            "published snapshot id or 'current'. Omit for the default current "
            "snapshot, or the legacy flat-parquet layout. Explicit ids never "
            "change current. Unlocked compatibility reads have no retention "
            "guarantee."
        ),
    )


@contextmanager
def _scoped_graph(graph: Path, snapshot: Optional[str]) -> Iterator[ByogGraph]:
    try:
        with retained_snapshot_read(
            graph, snapshot, allow_unlocked_managed=True
        ) as scope:
            yield scope.load_graph()
    except SnapshotReadError as error:
        typer.secho(str(error), fg=typer.colors.RED, err=True)
        raise typer.Exit(error.exit_code) from error


def _resolve_symbol(ents: pd.DataFrame, query: str) -> str | None:
    """Return the canonical title for a partial or exact query."""
    titles = ents["title"].astype(str)
    exact = ents[titles == query]
    if len(exact) == 1:
        return str(exact.iloc[0]["title"])

    if "type" in ents.columns:
        types = ents["type"].astype(str).str.lower()
        module_alias = ents[
            (types == "module")
            & (
                (titles == query)
                | (titles == f"{query}:{query}")
                | (titles == f"{query}:__module__")
                | titles.str.endswith(":" + query)
            )
        ]
        if len(module_alias) == 1:
            return str(module_alias.iloc[0]["title"])

    partial = ents[titles.str.contains(query, case=False, na=False)]
    if len(partial) == 1:
        return str(partial.iloc[0]["title"])
    return None


def callers(ents: pd.DataFrame, rels: pd.DataFrame, symbol: str) -> List[str]:
    title = _resolve_symbol(ents, symbol)
    if not title:
        return []
    mask = (rels["target"].astype(str) == title) & (rels["type"].astype(str) == "calls")
    return sorted(rels[mask]["source"].astype(str).unique().tolist())


def callees(ents: pd.DataFrame, rels: pd.DataFrame, symbol: str) -> List[str]:
    title = _resolve_symbol(ents, symbol)
    if not title:
        return []
    mask = (rels["source"].astype(str) == title) & (rels["type"].astype(str) == "calls")
    return sorted(rels[mask]["target"].astype(str).unique().tolist())


def types_used_by(ents: pd.DataFrame, rels: pd.DataFrame, symbol: str) -> List[str]:
    """Outgoing ``uses_type`` targets (sorted unique titles)."""
    title = _resolve_symbol(ents, symbol)
    if not title:
        return []
    mask = (rels["source"].astype(str) == title) & (
        rels["type"].astype(str) == "uses_type"
    )
    return sorted(rels[mask]["target"].astype(str).unique().tolist())


def type_users(ents: pd.DataFrame, rels: pd.DataFrame, symbol: str) -> List[str]:
    """Incoming ``uses_type`` sources (sorted unique titles)."""
    title = _resolve_symbol(ents, symbol)
    if not title:
        return []
    mask = (rels["target"].astype(str) == title) & (
        rels["type"].astype(str) == "uses_type"
    )
    return sorted(rels[mask]["source"].astype(str).unique().tolist())


def type_closure(
    ents: pd.DataFrame,
    rels: pd.DataFrame,
    symbol: str,
    *,
    direction: str = "dependencies",
    max_depth: int = DEFAULT_TYPE_CLOSURE_MAX_DEPTH,
    max_nodes: int = DEFAULT_TYPE_CLOSURE_MAX_NODES,
    max_edges: int = DEFAULT_TYPE_CLOSURE_MAX_EDGES,
) -> Dict[str, Any]:
    """Bounded cycle-safe transitive ``uses_type`` closure (delegates to pure BFS)."""
    title = _resolve_symbol(ents, symbol)
    return compute_uses_type_closure(
        rels,
        title,
        direction=direction,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )


def format_type_closure_human(result: Dict[str, Any]) -> str:
    """Stable human-readable type-closure report (shared by graph_query wrappers)."""
    lines: List[str] = []
    root = result.get("root")
    if not result.get("resolved"):
        lines.append("Not found" if root is None else f"root: {root}")
        lines.append(f"resolved: {bool(result.get('resolved'))}")
        lines.append(f"direction: {result.get('direction')}")
        lines.append(f"max_depth: {result.get('max_depth')}")
        lines.append("nodes (0/0):")
        lines.append("edges (0/0):")
        return "\n".join(lines)
    lines.append(f"root: {root}")
    lines.append(f"direction: {result.get('direction')}")
    lines.append(f"max_depth: {result.get('max_depth')}")
    n_ret = int(result.get("n_nodes_returned") or 0)
    n_tot = int(result.get("n_nodes_total") or 0)
    e_ret = int(result.get("n_edges_returned") or 0)
    e_tot = int(result.get("n_edges_total") or 0)
    trunc_n = " truncated" if result.get("nodes_truncated") else ""
    trunc_e = " truncated" if result.get("edges_truncated") else ""
    lines.append(f"nodes ({n_ret}/{n_tot}){trunc_n}:")
    for node in result.get("nodes") or []:
        lines.append(f"  {node.get('depth')}\t{node.get('title')}")
    lines.append(f"edges ({e_ret}/{e_tot}){trunc_e}:")
    for edge in result.get("edges") or []:
        lines.append(
            f"  {edge.get('depth')}\t{edge.get('source')} -> {edge.get('target')}\t{edge.get('id')}"
        )
    return "\n".join(lines)


def subgraph(
    ents: pd.DataFrame,
    rels: pd.DataFrame,
    symbol: str,
    *,
    direction: str = "both",
    max_depth: int = DEFAULT_SUBGRAPH_MAX_DEPTH,
    max_nodes: int = DEFAULT_SUBGRAPH_MAX_NODES,
    max_edges: int = DEFAULT_SUBGRAPH_MAX_EDGES,
    edge_types: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Bounded cycle-safe multi-hop induced subgraph (delegates to pure BFS)."""
    title = _resolve_symbol(ents, symbol)
    return compute_bounded_subgraph(
        ents,
        rels,
        title,
        direction=direction,
        max_depth=max_depth,
        max_nodes=max_nodes,
        max_edges=max_edges,
        edge_types=edge_types,
    )


def components(
    ents: pd.DataFrame,
    rels: pd.DataFrame,
    *,
    edge_types: Optional[Sequence[str]] = None,
    max_components: int = DEFAULT_COMPONENTS_MAX_COMPONENTS,
    max_nodes_per_component: int = DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT,
) -> Dict[str, Any]:
    """Weakly connected components over stored relationships (delegates to pure helper)."""
    return compute_weakly_connected_components(
        ents,
        rels,
        edge_types=edge_types,
        max_components=max_components,
        max_nodes_per_component=max_nodes_per_component,
    )


def strong_components(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
    *,
    edge_types: Optional[Sequence[str]] = None,
    max_components: int = DEFAULT_STRONG_COMPONENTS_MAX_COMPONENTS,
    max_nodes_per_component: int = DEFAULT_STRONG_COMPONENTS_MAX_NODES_PER_COMPONENT,
) -> Dict[str, Any]:
    """Directed strongly connected components (delegates to pure helper)."""
    return compute_strongly_connected_components(
        ents,
        rels,
        edge_types=edge_types,
        max_components=max_components,
        max_nodes_per_component=max_nodes_per_component,
    )


def condensation(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
    *,
    edge_types: Optional[Sequence[str]] = None,
    max_components: int = DEFAULT_CONDENSATION_MAX_COMPONENTS,
    max_nodes_per_component: int = DEFAULT_CONDENSATION_MAX_NODES_PER_COMPONENT,
    max_edges: int = DEFAULT_CONDENSATION_MAX_EDGES,
) -> Dict[str, Any]:
    """Directed SCC condensation DAG (delegates to pure helper)."""
    return compute_condensation_graph(
        ents,
        rels,
        edge_types=edge_types,
        max_components=max_components,
        max_nodes_per_component=max_nodes_per_component,
        max_edges=max_edges,
    )


def shortest_path(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
    source_title: Optional[str],
    target_title: Optional[str],
    *,
    edge_types: Optional[Sequence[str]] = None,
    max_depth: int = DEFAULT_SHORTEST_PATH_MAX_DEPTH,
) -> Dict[str, Any]:
    """Directed structural shortest path (delegates to pure helper)."""
    return compute_shortest_path(
        ents,
        rels,
        source_title,
        target_title,
        edge_types=edge_types,
        max_depth=max_depth,
    )


def degree_ranking(
    ents: pd.DataFrame,
    rels: pd.DataFrame,
    *,
    rank_by: str = "total",
    edge_types: Optional[Sequence[str]] = None,
    max_nodes: int = DEFAULT_DEGREE_RANKING_MAX_NODES,
) -> Dict[str, Any]:
    """Raw directed relationship-row degree ranking (delegates to pure helper)."""
    return compute_structural_degree_ranking(
        ents,
        rels,
        rank_by=rank_by,
        edge_types=edge_types,
        max_nodes=max_nodes,
    )


def dumps_subgraph_json(result: Dict[str, Any]) -> str:
    """Deterministic JSON for subgraph results (sort_keys, allow_nan=False)."""
    return json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def format_subgraph_human(result: Dict[str, Any]) -> str:
    """Stable human-readable subgraph report (shared by graph_query wrappers)."""
    lines: List[str] = []
    root = result.get("root")
    lines.append(f"root: {root if root is not None else 'null'}")
    lines.append(f"resolved: {bool(result.get('resolved'))}")
    lines.append(f"direction: {result.get('direction')}")
    lines.append(f"max_depth: {result.get('max_depth')}")
    lines.append(f"max_nodes: {result.get('max_nodes')}")
    lines.append(f"max_edges: {result.get('max_edges')}")
    edge_types = result.get("edge_types")
    if edge_types:
        lines.append("edge_types: " + ",".join(str(item) for item in edge_types))
    else:
        lines.append("edge_types: all")
    n_ret = int(result.get("n_nodes_returned") or 0)
    n_tot = int(result.get("n_nodes_total") or 0)
    e_ret = int(result.get("n_edges_returned") or 0)
    e_tot = int(result.get("n_edges_total") or 0)
    trunc_n = " truncated" if result.get("nodes_truncated") else ""
    trunc_e = " truncated" if result.get("edges_truncated") else ""
    lines.append(f"nodes ({n_ret}/{n_tot}){trunc_n}:")
    for node in result.get("nodes") or []:
        node_type = node.get("type")
        type_bit = f"\t{node_type}" if node_type is not None else ""
        lines.append(f"  {node.get('depth')}\t{node.get('title')}{type_bit}")
    lines.append(f"edges ({e_ret}/{e_tot}){trunc_e}:")
    for edge in result.get("edges") or []:
        lines.append(
            f"  {edge.get('depth')}\t{edge.get('source')} -> {edge.get('target')}\t"
            f"{edge.get('type')}\t{edge.get('id')}"
        )
    return "\n".join(lines)


def dumps_components_json(result: Dict[str, Any]) -> str:
    """Deterministic JSON for weakly-connected-components summaries."""
    return json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def format_components_human(result: Dict[str, Any]) -> str:
    """Stable human-readable components report (shared by graph_query wrappers)."""
    lines: List[str] = []
    edge_types = result.get("edge_types")
    if edge_types:
        lines.append("edge_types: " + ",".join(str(item) for item in edge_types))
    else:
        lines.append("edge_types: all")
    lines.append(f"max_components: {result.get('max_components')}")
    lines.append(
        f"max_nodes_per_component: {result.get('max_nodes_per_component')}"
    )
    n_ret = int(result.get("n_components_returned") or 0)
    n_tot = int(result.get("n_components_total") or 0)
    trunc_c = " truncated" if result.get("components_truncated") else ""
    lines.append(f"components ({n_ret}/{n_tot}){trunc_c}:")
    lines.append(f"n_nodes_total: {int(result.get('n_nodes_total') or 0)}")
    lines.append(f"n_edges_total: {int(result.get('n_edges_total') or 0)}")
    lines.append(
        f"n_entity_nodes_total: {int(result.get('n_entity_nodes_total') or 0)}"
    )
    lines.append(
        "n_endpoint_only_nodes_total: "
        f"{int(result.get('n_endpoint_only_nodes_total') or 0)}"
    )
    lines.append(
        "nodes_truncated: "
        f"{'true' if result.get('nodes_truncated') else 'false'}"
    )
    for comp in result.get("components") or []:
        nt = " truncated" if comp.get("nodes_truncated") else ""
        lines.append(
            f"  {comp.get('representative')} "
            f"nodes ({int(comp.get('n_nodes_returned') or 0)}/"
            f"{int(comp.get('n_nodes_total') or 0)}){nt} "
            f"edges {int(comp.get('n_edges_total') or 0)} "
            f"entity {int(comp.get('n_entity_nodes') or 0)} "
            f"endpoint-only {int(comp.get('n_endpoint_only_nodes') or 0)}"
        )
        for title in comp.get("nodes") or []:
            lines.append(f"    {title}")
    return "\n".join(lines)


def dumps_strong_components_json(result: Dict[str, Any]) -> str:
    """Deterministic JSON for directed strongly-connected-components summaries."""
    return json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def format_strong_components_human(result: Dict[str, Any]) -> str:
    """Stable human-readable strong-components report."""
    lines: List[str] = []
    edge_types = result.get("edge_types")
    if edge_types:
        lines.append("edge_types: " + ",".join(str(item) for item in edge_types))
    else:
        lines.append("edge_types: all")
    lines.append(f"max_components: {result.get('max_components')}")
    lines.append(
        f"max_nodes_per_component: {result.get('max_nodes_per_component')}"
    )
    n_ret = int(result.get("n_components_returned") or 0)
    n_tot = int(result.get("n_components_total") or 0)
    trunc_c = " truncated" if result.get("components_truncated") else ""
    lines.append(f"components ({n_ret}/{n_tot}){trunc_c}:")
    lines.append(f"n_nodes_total: {int(result.get('n_nodes_total') or 0)}")
    lines.append(f"n_edges_total: {int(result.get('n_edges_total') or 0)}")
    lines.append(
        f"n_internal_edges_total: {int(result.get('n_internal_edges_total') or 0)}"
    )
    lines.append(
        "n_cross_component_edges_total: "
        f"{int(result.get('n_cross_component_edges_total') or 0)}"
    )
    lines.append(
        f"n_self_loop_edges_total: {int(result.get('n_self_loop_edges_total') or 0)}"
    )
    lines.append(
        "n_cyclic_components_total: "
        f"{int(result.get('n_cyclic_components_total') or 0)}"
    )
    lines.append(
        f"n_entity_nodes_total: {int(result.get('n_entity_nodes_total') or 0)}"
    )
    lines.append(
        "n_endpoint_only_nodes_total: "
        f"{int(result.get('n_endpoint_only_nodes_total') or 0)}"
    )
    lines.append(
        "nodes_truncated: "
        f"{'true' if result.get('nodes_truncated') else 'false'}"
    )
    for comp in result.get("components") or []:
        nt = " truncated" if comp.get("nodes_truncated") else ""
        cyclic = "cyclic" if comp.get("is_cyclic") else "acyclic"
        lines.append(
            f"  {comp.get('representative')} {cyclic} "
            f"nodes ({int(comp.get('n_nodes_returned') or 0)}/"
            f"{int(comp.get('n_nodes_total') or 0)}){nt} "
            f"internal {int(comp.get('n_internal_edges_total') or 0)} "
            f"self-loops {int(comp.get('n_self_loop_edges_total') or 0)} "
            f"entity {int(comp.get('n_entity_nodes') or 0)} "
            f"endpoint-only {int(comp.get('n_endpoint_only_nodes') or 0)}"
        )
        for title in comp.get("nodes") or []:
            lines.append(f"    {title}")
    return "\n".join(lines)


def dumps_condensation_json(result: Dict[str, Any]) -> str:
    """Deterministic JSON for directed SCC condensation-DAG summaries."""
    return json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def dumps_shortest_path_json(result: Dict[str, Any]) -> str:
    """Deterministic JSON for directed structural shortest-path results."""
    return json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def format_shortest_path_human(result: Dict[str, Any]) -> str:
    """Stable human-readable shortest-path report."""
    lines: List[str] = []
    lines.append(f"status: {result.get('status')}")
    source = result.get("source")
    target = result.get("target")
    lines.append("source: " + ("null" if source is None else str(source)))
    lines.append("target: " + ("null" if target is None else str(target)))
    lines.append(
        "source_resolved: "
        + ("true" if result.get("source_resolved") else "false")
    )
    lines.append(
        "target_resolved: "
        + ("true" if result.get("target_resolved") else "false")
    )
    lines.append("found: " + ("true" if result.get("found") else "false"))
    edge_types = result.get("edge_types")
    if edge_types:
        lines.append("edge_types: " + ",".join(str(item) for item in edge_types))
    else:
        lines.append("edge_types: all")
    lines.append(f"max_depth: {result.get('max_depth')}")
    distance = result.get("distance")
    lines.append(
        "distance: " + ("null" if distance is None else str(distance))
    )
    lines.append(f"n_nodes_returned: {int(result.get('n_nodes_returned') or 0)}")
    lines.append(f"n_steps_returned: {int(result.get('n_steps_returned') or 0)}")
    lines.append(
        "n_relationship_rows_on_path_total: "
        f"{int(result.get('n_relationship_rows_on_path_total') or 0)}"
    )
    lines.append("nodes:")
    for title in result.get("nodes") or []:
        lines.append(f"  {title}")
    lines.append("steps:")
    for step in result.get("steps") or []:
        lines.append(
            f"  {step.get('source')} -> {step.get('target')} "
            f"rows {int(step.get('n_relationship_rows_total') or 0)}"
        )
    if result.get("status") == "not_found_within_max_depth":
        lines.append("not found within max_depth")
    return "\n".join(lines)


def format_condensation_human(result: Dict[str, Any]) -> str:
    """Stable human-readable condensation-DAG report."""
    lines: List[str] = []
    edge_types = result.get("edge_types")
    if edge_types:
        lines.append("edge_types: " + ",".join(str(item) for item in edge_types))
    else:
        lines.append("edge_types: all")
    lines.append(f"max_components: {result.get('max_components')}")
    lines.append(
        f"max_nodes_per_component: {result.get('max_nodes_per_component')}"
    )
    lines.append(f"max_edges: {result.get('max_edges')}")
    n_ret = int(result.get("n_components_returned") or 0)
    n_tot = int(result.get("n_components_total") or 0)
    trunc_c = " truncated" if result.get("components_truncated") else ""
    lines.append(f"components ({n_ret}/{n_tot}){trunc_c}:")
    lines.append(f"n_nodes_total: {int(result.get('n_nodes_total') or 0)}")
    lines.append(f"n_edges_total: {int(result.get('n_edges_total') or 0)}")
    lines.append(
        f"n_internal_edges_total: {int(result.get('n_internal_edges_total') or 0)}"
    )
    lines.append(
        "n_cross_component_edges_total: "
        f"{int(result.get('n_cross_component_edges_total') or 0)}"
    )
    lines.append(
        f"n_self_loop_edges_total: {int(result.get('n_self_loop_edges_total') or 0)}"
    )
    lines.append(
        "n_cyclic_components_total: "
        f"{int(result.get('n_cyclic_components_total') or 0)}"
    )
    lines.append(
        f"n_entity_nodes_total: {int(result.get('n_entity_nodes_total') or 0)}"
    )
    lines.append(
        "n_endpoint_only_nodes_total: "
        f"{int(result.get('n_endpoint_only_nodes_total') or 0)}"
    )
    lines.append(
        "n_condensation_edges_total: "
        f"{int(result.get('n_condensation_edges_total') or 0)}"
    )
    lines.append(
        "n_condensation_edges_eligible_total: "
        f"{int(result.get('n_condensation_edges_eligible_total') or 0)}"
    )
    lines.append(
        "n_condensation_edges_returned: "
        f"{int(result.get('n_condensation_edges_returned') or 0)}"
    )
    lines.append(
        "nodes_truncated: "
        f"{'true' if result.get('nodes_truncated') else 'false'}"
    )
    lines.append(
        "edges_truncated: "
        f"{'true' if result.get('edges_truncated') else 'false'}"
    )
    for comp in result.get("components") or []:
        nt = " truncated" if comp.get("nodes_truncated") else ""
        cyclic = "cyclic" if comp.get("is_cyclic") else "acyclic"
        lines.append(
            f"  {comp.get('representative')} {cyclic} "
            f"nodes ({int(comp.get('n_nodes_returned') or 0)}/"
            f"{int(comp.get('n_nodes_total') or 0)}){nt} "
            f"internal {int(comp.get('n_internal_edges_total') or 0)} "
            f"self-loops {int(comp.get('n_self_loop_edges_total') or 0)} "
            f"entity {int(comp.get('n_entity_nodes') or 0)} "
            f"endpoint-only {int(comp.get('n_endpoint_only_nodes') or 0)}"
        )
        for title in comp.get("nodes") or []:
            lines.append(f"    {title}")
    e_ret = int(result.get("n_condensation_edges_returned") or 0)
    e_tot = int(result.get("n_condensation_edges_total") or 0)
    trunc_e = " truncated" if result.get("edges_truncated") else ""
    lines.append(f"edges ({e_ret}/{e_tot}){trunc_e}:")
    for edge in result.get("edges") or []:
        lines.append(
            f"  {edge.get('source')} -> {edge.get('target')} "
            f"rows {int(edge.get('n_relationship_rows_total') or 0)}"
        )
    return "\n".join(lines)


def dumps_degree_ranking_json(result: Dict[str, Any]) -> str:
    """Deterministic JSON for directed degree-ranking summaries."""
    return json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def format_degree_ranking_human(result: Dict[str, Any]) -> str:
    """Stable human-readable degree-ranking report."""
    lines: List[str] = []
    lines.append(f"rank_by: {result.get('rank_by')}")
    edge_types = result.get("edge_types")
    if edge_types:
        lines.append("edge_types: " + ",".join(str(item) for item in edge_types))
    else:
        lines.append("edge_types: all")
    lines.append(f"max_nodes: {result.get('max_nodes')}")
    n_ret = int(result.get("n_nodes_returned") or 0)
    n_tot = int(result.get("n_nodes_total") or 0)
    trunc = " truncated" if result.get("nodes_truncated") else ""
    lines.append(f"nodes ({n_ret}/{n_tot}){trunc}:")
    lines.append(f"n_edges_total: {int(result.get('n_edges_total') or 0)}")
    lines.append(
        f"n_entity_nodes_total: {int(result.get('n_entity_nodes_total') or 0)}"
    )
    lines.append(
        "n_endpoint_only_nodes_total: "
        f"{int(result.get('n_endpoint_only_nodes_total') or 0)}"
    )
    lines.append(f"sum_in_degree: {int(result.get('sum_in_degree') or 0)}")
    lines.append(f"sum_out_degree: {int(result.get('sum_out_degree') or 0)}")
    lines.append(f"sum_total_degree: {int(result.get('sum_total_degree') or 0)}")
    for node in result.get("nodes") or []:
        kind = "entity" if node.get("is_entity") else "endpoint-only"
        lines.append(
            f"  {node.get('title')}  incoming {int(node.get('in_degree') or 0)}  "
            f"outgoing {int(node.get('out_degree') or 0)}  "
            f"total {int(node.get('total_degree') or 0)}  {kind}"
        )
    return "\n".join(lines)


def neighbors(ents: pd.DataFrame, rels: pd.DataFrame, symbol: str) -> Dict[str, List[str]]:
    title = _resolve_symbol(ents, symbol)
    if not title:
        return {"incoming": [], "outgoing": []}
    inc = rels[(rels["target"].astype(str) == title)]["source"].astype(str).unique().tolist()
    out = rels[(rels["source"].astype(str) == title)]["target"].astype(str).unique().tolist()
    return {"incoming": sorted(inc), "outgoing": sorted(out)}


def dependency_order(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
) -> List[str]:
    """Deterministic containment order over persisted ``contains`` rows."""
    return compute_containment_dependency_order(ents, rels)


def dumps_dependency_order_json(result: List[str]) -> str:
    """Deterministic JSON for the containment-order title list."""
    return json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
        sort_keys=True,
        allow_nan=False,
    )


def format_dependency_order_human(result: List[str]) -> str:
    """One title per line in producer order. Empty graphs yield empty text."""
    return "\n".join(result)


def impact(ents: pd.DataFrame, rels: pd.DataFrame, symbol: str) -> List[str]:
    """Transitive callers (who would be affected if this symbol changes)."""
    title = _resolve_symbol(ents, symbol)
    if not title:
        return []
    # Build reverse call graph
    from collections import defaultdict, deque

    rev: Dict[str, List[str]] = defaultdict(list)
    call_mask = rels["type"].astype(str) == "calls"
    for _, row in rels[call_mask].astype(str).iterrows():
        rev[row["target"]].append(row["source"])

    # BFS from the symbol
    seen = set()
    q = deque([title])
    while q:
        cur = q.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        for pred in rev.get(cur, []):
            if pred not in seen:
                q.append(pred)
    seen.discard(title)
    return sorted(seen)


def symbol_lookup(ents: pd.DataFrame, query: str) -> Dict[str, Any] | None:
    title = _resolve_symbol(ents, query)
    if not title:
        return None
    row = ents[ents["title"].astype(str) == title].iloc[0]
    snippet = row.get("snippet", None) if "snippet" in row else None
    snippet_preview = str(snippet)[:200] if snippet else None
    return {
        "title": title,
        "type": row.get("type"),
        "description": row.get("description"),
        "source_file": row.get("source_file"),
        "span": row.get("span"),
        "snippet_preview": snippet_preview,
    }


@app.command("callers")
def cli_callers(
    symbol: str,
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
):
    with _scoped_graph(graph, snapshot) as g:
        print("\n".join(g.callers(symbol)))


@app.command("callees")
def cli_callees(
    symbol: str,
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
):
    with _scoped_graph(graph, snapshot) as g:
        print("\n".join(g.callees(symbol)))


@app.command("types-used-by")
def cli_types_used_by(
    symbol: str,
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_output: bool = typer.Option(False, "--json"),
):
    """Outgoing uses_type targets for a symbol (configured type-use overlay)."""
    with _scoped_graph(graph, snapshot) as g:
        result = g.types_used_by(symbol)
        if json_output:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print("\n".join(result))


@app.command("type-users")
def cli_type_users(
    symbol: str,
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_output: bool = typer.Option(False, "--json"),
):
    """Incoming uses_type sources for a type/symbol (configured type-use overlay)."""
    with _scoped_graph(graph, snapshot) as g:
        result = g.type_users(symbol)
        if json_output:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print("\n".join(result))


@app.command("type-closure")
def cli_type_closure(
    symbol: str,
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    direction: str = typer.Option(
        "dependencies",
        "--direction",
        help="dependencies (outgoing), users (incoming), or both",
    ),
    max_depth: int = typer.Option(
        DEFAULT_TYPE_CLOSURE_MAX_DEPTH,
        "--max-depth",
        help="Maximum BFS depth (non-negative)",
    ),
    max_nodes: int = typer.Option(
        DEFAULT_TYPE_CLOSURE_MAX_NODES,
        "--max-nodes",
        help="Max nodes returned (exact totals still reported)",
    ),
    max_edges: int = typer.Option(
        DEFAULT_TYPE_CLOSURE_MAX_EDGES,
        "--max-edges",
        help="Max edges returned (exact totals still reported)",
    ),
    json_output: bool = typer.Option(False, "--json"),
):
    """Bounded cycle-safe transitive uses_type closure (consumer-only)."""
    try:
        with _scoped_graph(graph, snapshot) as g:
            result = g.type_closure(
                symbol,
                direction=direction,
                max_depth=max_depth,
                max_nodes=max_nodes,
                max_edges=max_edges,
            )
            if json_output:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(format_type_closure_human(result))
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e


@app.command("subgraph")
def cli_subgraph(
    symbol: str,
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    direction: str = typer.Option(
        "both",
        "--direction",
        help="outgoing, incoming, or both (reachability only)",
    ),
    max_depth: int = typer.Option(
        DEFAULT_SUBGRAPH_MAX_DEPTH,
        "--max-depth",
        help=f"Maximum BFS depth (0..{HARD_MAX_SUBGRAPH_DEPTH})",
    ),
    max_nodes: int = typer.Option(
        DEFAULT_SUBGRAPH_MAX_NODES,
        "--max-nodes",
        help=f"Max nodes returned (1..{HARD_MAX_SUBGRAPH_NODES}); totals stay exact",
    ),
    max_edges: int = typer.Option(
        DEFAULT_SUBGRAPH_MAX_EDGES,
        "--max-edges",
        help=f"Max edges returned (0..{HARD_MAX_SUBGRAPH_EDGES}); totals stay exact",
    ),
    edge_type: List[str] = typer.Option(
        [],
        "--edge-type",
        help="Exact relationship-type allow-list (repeatable). Omit for all types.",
    ),
    json_output: bool = typer.Option(False, "--json"),
    dot_output: bool = typer.Option(
        False,
        "--dot",
        help=(
            "Write deterministic Graphviz DOT to stdout. Interchange only; "
            "does not invoke Graphviz or render an image. Mutually exclusive "
            "with --json."
        ),
    ),
):
    """Bounded cycle-safe multi-hop induced subgraph (structural exploration only).

    ``--dot`` is Graphviz DOT interchange on stdout: Graphviz is not invoked
    and no image is rendered. ``--json`` and ``--dot`` are mutually exclusive.
    """
    if json_output and dot_output:
        typer.secho(
            "--json and --dot are mutually exclusive",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    try:
        with _scoped_graph(graph, snapshot) as g:
            result = g.subgraph(
                symbol,
                direction=direction,
                max_depth=max_depth,
                max_nodes=max_nodes,
                max_edges=max_edges,
                edge_types=edge_type or None,
            )
            if json_output:
                print(dumps_subgraph_json(result), flush=True)
            elif dot_output:
                sys.stdout.write(dumps_subgraph_dot(result))
                sys.stdout.flush()
            else:
                print(format_subgraph_human(result), flush=True)
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e


@app.command("components")
def cli_components(
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    max_components: int = typer.Option(
        DEFAULT_COMPONENTS_MAX_COMPONENTS,
        "--max-components",
        help=f"Max components returned (1..{HARD_MAX_COMPONENTS}); totals stay exact",
    ),
    max_nodes_per_component: int = typer.Option(
        DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT,
        "--max-nodes-per-component",
        help=(
            f"Max node titles per returned component "
            f"(1..{HARD_MAX_COMPONENT_NODES}); totals stay exact"
        ),
    ),
    edge_type: List[str] = typer.Option(
        [],
        "--edge-type",
        help="Exact relationship-type allow-list (repeatable). Omit for all types.",
    ),
    json_output: bool = typer.Option(False, "--json"),
):
    """Weakly connected components over persisted relationships (topology only).

    Structural grouping summary: not semantic community detection, Leiden,
    centrality, architecture inference, GraphRAG, or natural-language analysis.
    """
    try:
        with _scoped_graph(graph, snapshot) as g:
            result = g.components(
                edge_types=edge_type or None,
                max_components=max_components,
                max_nodes_per_component=max_nodes_per_component,
            )
            if json_output:
                print(dumps_components_json(result), flush=True)
            else:
                print(format_components_human(result), flush=True)
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e


@app.command("strong-components")
def cli_strong_components(
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    max_components: int = typer.Option(
        DEFAULT_STRONG_COMPONENTS_MAX_COMPONENTS,
        "--max-components",
        help=(
            f"Max components returned (1..{HARD_MAX_STRONG_COMPONENTS}); "
            "totals stay exact"
        ),
    ),
    max_nodes_per_component: int = typer.Option(
        DEFAULT_STRONG_COMPONENTS_MAX_NODES_PER_COMPONENT,
        "--max-nodes-per-component",
        help=(
            f"Max node titles per returned component "
            f"(1..{HARD_MAX_STRONG_COMPONENT_NODES}); totals stay exact"
        ),
    ),
    edge_type: List[str] = typer.Option(
        [],
        "--edge-type",
        help="Exact relationship-type allow-list (repeatable). Omit for all types.",
    ),
    json_output: bool = typer.Option(False, "--json"),
):
    """Directed strongly connected components over persisted relationships.

    Exact mutual-reachability grouping: not weak components, semantic
    communities, Leiden, architecture inference, GraphRAG, or a runtime
    recursion/deadlock proof.
    """
    try:
        with _scoped_graph(graph, snapshot) as g:
            result = g.strong_components(
                edge_types=edge_type or None,
                max_components=max_components,
                max_nodes_per_component=max_nodes_per_component,
            )
            if json_output:
                print(dumps_strong_components_json(result), flush=True)
            else:
                print(format_strong_components_human(result), flush=True)
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e


@app.command("condensation")
def cli_condensation(
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    max_components: int = typer.Option(
        DEFAULT_CONDENSATION_MAX_COMPONENTS,
        "--max-components",
        help=(
            f"Max components returned (1..{HARD_MAX_CONDENSATION_COMPONENTS}); "
            "totals stay exact"
        ),
    ),
    max_nodes_per_component: int = typer.Option(
        DEFAULT_CONDENSATION_MAX_NODES_PER_COMPONENT,
        "--max-nodes-per-component",
        help=(
            f"Max node titles per returned component "
            f"(1..{HARD_MAX_CONDENSATION_COMPONENT_NODES}); totals stay exact"
        ),
    ),
    max_edges: int = typer.Option(
        DEFAULT_CONDENSATION_MAX_EDGES,
        "--max-edges",
        help=(
            f"Max condensation edges returned "
            f"(0..{HARD_MAX_CONDENSATION_EDGES}); totals stay exact"
        ),
    ),
    edge_type: List[str] = typer.Option(
        [],
        "--edge-type",
        help="Exact relationship-type allow-list (repeatable). Omit for all types.",
    ),
    json_output: bool = typer.Option(False, "--json"),
    dot_output: bool = typer.Option(
        False,
        "--dot",
        help=(
            "Write deterministic Graphviz DOT to stdout. Interchange only; "
            "does not invoke Graphviz or render an image. Mutually exclusive "
            "with --json."
        ),
    ),
):
    """Directed SCC condensation DAG over persisted relationships.

    Exact mutual-reachability SCCs plus one deterministic topological
    presentation of the acyclic condensation: not weak components, cycle
    enumeration, a unique rank, semantic communities, Leiden, architecture
    inference, GraphRAG, or a runtime recursion/deadlock proof.

    ``--dot`` is Graphviz DOT interchange on stdout: Graphviz is not invoked
    and no image is rendered. ``--json`` and ``--dot`` are mutually exclusive.
    """
    if json_output and dot_output:
        typer.secho(
            "--json and --dot are mutually exclusive",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    try:
        with _scoped_graph(graph, snapshot) as g:
            result = g.condensation(
                edge_types=edge_type or None,
                max_components=max_components,
                max_nodes_per_component=max_nodes_per_component,
                max_edges=max_edges,
            )
            if json_output:
                print(dumps_condensation_json(result), flush=True)
            elif dot_output:
                sys.stdout.write(dumps_condensation_dot(result))
                sys.stdout.flush()
            else:
                print(format_condensation_human(result), flush=True)
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e


@app.command("shortest-path")
def cli_shortest_path(
    source: str = typer.Argument(..., help="Source symbol or module"),
    target: str = typer.Argument(..., help="Target symbol or module"),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    max_depth: int = typer.Option(
        DEFAULT_SHORTEST_PATH_MAX_DEPTH,
        "--max-depth",
        help=f"Maximum directed hops (0..{HARD_MAX_SHORTEST_PATH_DEPTH})",
    ),
    edge_type: List[str] = typer.Option(
        [],
        "--edge-type",
        help="Exact relationship-type allow-list (repeatable). Omit for all types.",
    ),
    json_output: bool = typer.Option(False, "--json"),
):
    """Directed structural shortest path over persisted relationships.

    Stored ``source -> target`` orientation only. A reverse-direction query
    is expressed by swapping the requested endpoints. Not provenance,
    execution evidence, semantic dependency, GraphRAG, or a UI. There is
    no DOT in this milestone.
    """
    try:
        with _scoped_graph(graph, snapshot) as g:
            result = g.shortest_path(
                source,
                target,
                edge_types=edge_type or None,
                max_depth=max_depth,
            )
            if json_output:
                print(dumps_shortest_path_json(result), flush=True)
            else:
                print(format_shortest_path_human(result), flush=True)
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e


@app.command("degree-ranking")
def cli_degree_ranking(
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    rank_by: str = typer.Option(
        "total",
        "--rank-by",
        help="Rank by total, incoming, or outgoing degree",
    ),
    max_nodes: int = typer.Option(
        DEFAULT_DEGREE_RANKING_MAX_NODES,
        "--max-nodes",
        help=(
            f"Max node rows returned (1..{HARD_MAX_DEGREE_RANKING_NODES}); "
            "totals stay exact"
        ),
    ),
    edge_type: List[str] = typer.Option(
        [],
        "--edge-type",
        help="Exact relationship-type allow-list (repeatable). Omit for all types.",
    ),
    json_output: bool = typer.Option(False, "--json"),
):
    """Raw directed relationship-row degree ranking (structural accounting only).

    Not PageRank, betweenness, closeness, eigenvector centrality, semantic
    importance, architecture inference, community detection, or GraphRAG.
    """
    try:
        with _scoped_graph(graph, snapshot) as g:
            result = g.degree_ranking(
                rank_by=rank_by,
                edge_types=edge_type or None,
                max_nodes=max_nodes,
            )
            if json_output:
                print(dumps_degree_ranking_json(result), flush=True)
            else:
                print(format_degree_ranking_human(result), flush=True)
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e


@app.command("neighbors")
def cli_neighbors(
    symbol: str,
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
):
    with _scoped_graph(graph, snapshot) as g:
        n = g.neighbors(symbol)
        print("incoming:", n["incoming"])
        print("outgoing:", n["outgoing"])


@app.command("dependency-order")
def cli_dep_order(
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_output: bool = typer.Option(False, "--json"),
):
    """Deterministic structural containment order over persisted contains rows.

    Source appears before target across strongly connected components.
    Not a build, import, call, or semantic dependency order. Unbounded
    full list; no DOT and not an MCP tool.
    """
    try:
        with _scoped_graph(graph, snapshot) as g:
            result = g.dependency_order()
            if json_output:
                print(dumps_dependency_order_json(result), flush=True)
            else:
                human = format_dependency_order_human(result)
                if human:
                    print(human, flush=True)
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(2) from e


@app.command("impact")
def cli_impact(
    symbol: str,
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
):
    with _scoped_graph(graph, snapshot) as g:
        print("\n".join(g.impact(symbol)))


@app.command("symbol")
def cli_symbol(
    query: str,
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
):
    with _scoped_graph(graph, snapshot) as g:
        res = g.symbol(query)
        if res:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print("Not found")


@app.command("observations")
def cli_observations(
    query: str,
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON (for agent loops / diagnostics)"),
):
    """Show weak/ambiguous/container call observations for a symbol or module.

    Lightweight diagnostic for the Python resolver (annotations, guards, builtins)
    without needing a full context pack.

    Use --json for programmatic consumption by agents.
    """
    with _scoped_graph(graph, snapshot) as g:
        obs = g.observations(query)
        if json_output:
            print(json.dumps(obs, indent=2, ensure_ascii=False))
            return
        if not obs:
            print(f"No observations for {query}")
            return
        for o in obs:
            src = o.get("source", "?")
            tgt = o.get("display_target", "?")
            conf = o.get("confidence", "?")
            reason = o.get("reason", "")
            prov = ""
            sf = o.get("source_file")
            sp = o.get("span")
            if sf:
                prov = f"{sf}:{sp}" if sp else sf
            line = f"{src} -> {tgt}  conf={conf}"
            if reason:
                line += f"  [{reason}]"
            print(line)
            if prov:
                print(f"    {prov}")


if __name__ == "__main__":
    app()
