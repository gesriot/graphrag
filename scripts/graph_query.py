#!/usr/bin/env python
"""
Local graph queries over BYOG parquets (no external API).

Provides:
- callers(symbol)
- callees(symbol)
- neighbors(symbol)
- dependency_order()
- impact(symbol)
- symbol(query)
- observations(symbol_or_module)   # weak/ambiguous/container resolver diagnostics

Designed to be used from agent loops, context-pack, or directly from the shell.

Example:
    uv run python scripts/graph_query.py callers sim:run_simulation --graph byog_mini_game
    uv run python scripts/graph_query.py observations sim:run_simulation --graph byog_mini_game
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Dict, Any

import pandas as pd
import sys
import typer

# Support both `python -m scripts.xxx` and direct `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).parent))
from byog_graph import ByogGraph, load_graph  # re-export for backward compat

app = typer.Typer(help="Local BYOG graph queries (callers, callees, impact, etc.)")


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
def cli_callers(symbol: str, graph: Path = typer.Option(Path("byog_mini_game"), "--graph")):
    g = ByogGraph(graph)
    print("\n".join(g.callers(symbol)))


@app.command("callees")
def cli_callees(symbol: str, graph: Path = typer.Option(Path("byog_mini_game"), "--graph")):
    g = ByogGraph(graph)
    print("\n".join(g.callees(symbol)))


@app.command("neighbors")
def cli_neighbors(symbol: str, graph: Path = typer.Option(Path("byog_mini_game"), "--graph")):
    g = ByogGraph(graph)
    n = g.neighbors(symbol)
    print("incoming:", n["incoming"])
    print("outgoing:", n["outgoing"])


@app.command("dependency-order")
def cli_dep_order(graph: Path = typer.Option(Path("byog_mini_game"), "--graph")):
    g = ByogGraph(graph)
    for t in g.dependency_order():
        print(t)


@app.command("impact")
def cli_impact(symbol: str, graph: Path = typer.Option(Path("byog_mini_game"), "--graph")):
    g = ByogGraph(graph)
    print("\n".join(g.impact(symbol)))


@app.command("symbol")
def cli_symbol(query: str, graph: Path = typer.Option(Path("byog_mini_game"), "--graph")):
    g = ByogGraph(graph)
    res = g.symbol(query)
    if res:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    else:
        print("Not found")


@app.command("observations")
def cli_observations(
    query: str,
    graph: Path = typer.Option(Path("byog_mini_game"), "--graph"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON (for agent loops / diagnostics)"),
):
    """Show weak/ambiguous/container call observations for a symbol or module.

    Lightweight diagnostic for the Python resolver (annotations, guards, builtins)
    without needing a full context pack.

    Use --json for programmatic consumption by agents.
    """
    g = ByogGraph(graph)
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
