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
- neighbors(symbol)
- dependency_order()
- impact(symbol)
- symbol(query)
- observations(symbol_or_module)   # weak/ambiguous/container resolver diagnostics

Designed to be used from agent loops, context-pack, or directly from the shell.

Example:
    uv run python scripts/graph_query.py callers sim:run_simulation --graph byog_mini_game
    uv run python scripts/graph_query.py types-used-by ini:ini_parse --graph byog_inih
    uv run python scripts/graph_query.py type-closure ini:ini_parse --direction dependencies --max-depth 2
    uv run python scripts/graph_query.py subgraph sim:run_simulation --graph byog_mini_game --direction both
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
    DEFAULT_SUBGRAPH_MAX_DEPTH,
    DEFAULT_SUBGRAPH_MAX_EDGES,
    DEFAULT_SUBGRAPH_MAX_NODES,
    DEFAULT_TYPE_CLOSURE_MAX_DEPTH,
    DEFAULT_TYPE_CLOSURE_MAX_EDGES,
    DEFAULT_TYPE_CLOSURE_MAX_NODES,
    HARD_MAX_SUBGRAPH_DEPTH,
    HARD_MAX_SUBGRAPH_EDGES,
    HARD_MAX_SUBGRAPH_NODES,
    ByogGraph,
    compute_bounded_subgraph,
    compute_uses_type_closure,
    load_graph,
)
from graphrag_code.snapshot_read import (
    SnapshotReadError,
    retained_snapshot_read,
)

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


def neighbors(ents: pd.DataFrame, rels: pd.DataFrame, symbol: str) -> Dict[str, List[str]]:
    title = _resolve_symbol(ents, symbol)
    if not title:
        return {"incoming": [], "outgoing": []}
    inc = rels[(rels["target"].astype(str) == title)]["source"].astype(str).unique().tolist()
    out = rels[(rels["source"].astype(str) == title)]["target"].astype(str).unique().tolist()
    return {"incoming": sorted(inc), "outgoing": sorted(out)}


def dependency_order(ents: pd.DataFrame, rels: pd.DataFrame) -> List[str]:
    """Very simple topological order based on 'contains' edges (modules/files first)."""
    contains = rels[rels["type"].astype(str) == "contains"][["source", "target"]].astype(str)
    # Build graph of containment (source contains target)
    from collections import defaultdict, deque

    graph: Dict[str, List[str]] = defaultdict(list)
    indeg: Dict[str, int] = defaultdict(int)

    all_nodes = set(ents["title"].astype(str))
    for _, row in contains.iterrows():
        src, tgt = row["source"], row["target"]
        graph[src].append(tgt)
        indeg[tgt] += 1
        all_nodes.add(src)
        all_nodes.add(tgt)

    q = deque([n for n in all_nodes if indeg[n] == 0])
    order = []
    while q:
        n = q.popleft()
        order.append(n)
        for nei in graph[n]:
            indeg[nei] -= 1
            if indeg[nei] == 0:
                q.append(nei)
    # If cycle or disconnected, just return what we have + remaining
    remaining = sorted(all_nodes - set(order))
    return order + remaining


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
):
    """Bounded cycle-safe multi-hop induced subgraph (structural exploration only)."""
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
            else:
                print(format_subgraph_human(result), flush=True)
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
):
    with _scoped_graph(graph, snapshot) as g:
        for t in g.dependency_order():
            print(t)


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
