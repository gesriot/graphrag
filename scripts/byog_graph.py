"""
Common BYOG graph loader and model (extracted from context_pack and graph_query).

Provides a clean ByogGraph class that both tools can use.

This reduces duplication and makes it easier to add local queries, module packs, etc.

All deterministic, no external API.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd


class ByogGraph:
    """Lightweight in-memory view over a BYOG (entities, relationships, text_units)."""

    def __init__(self, graph_dir: Path):
        self.root = Path(graph_dir)
        out = self.root / "output"
        self.ents: pd.DataFrame = pd.read_parquet(out / "entities.parquet")
        self.rels: pd.DataFrame = pd.read_parquet(out / "relationships.parquet")
        tus_path = out / "text_units.parquet"
        self.tus: pd.DataFrame = (
            pd.read_parquet(tus_path) if tus_path.exists() else pd.DataFrame()
        )

        # Precompute for fast resolve
        self._title_to_row: Dict[str, pd.Series] = {
            str(row["title"]): row for _, row in self.ents.iterrows()
        }

    @property
    def titles(self) -> List[str]:
        return self.ents["title"].astype(str).tolist()

    def resolve(self, query: str) -> Optional[str]:
        """Return canonical title for exact/partial/module-alias query."""
        titles = self.ents["title"].astype(str)
        exact = self.ents[titles == query]
        if len(exact) == 1:
            return str(exact.iloc[0]["title"])

        # module alias support (e.g. "sim" -> "sim:sim")
        if "type" in self.ents.columns:
            types = self.ents["type"].astype(str).str.lower()
            module_alias = self.ents[
                (types == "module")
                & (
                    (titles == query)
                    | (titles == f"{query}:{query}")
                    | titles.str.endswith(":" + query)
                )
            ]
            if len(module_alias) == 1:
                return str(module_alias.iloc[0]["title"])

        partial = self.ents[titles.str.contains(query, case=False, na=False)]
        if len(partial) == 1:
            return str(partial.iloc[0]["title"])
        return None

    def get_entity(self, title: str) -> Optional[pd.Series]:
        t = self.resolve(title)
        if t and t in self._title_to_row:
            return self._title_to_row[t]
        return None

    def callers(self, symbol: str) -> List[str]:
        title = self.resolve(symbol)
        if not title:
            return []
        mask = (self.rels["target"].astype(str) == title) & (
            self.rels["type"].astype(str) == "calls"
        )
        return sorted(self.rels[mask]["source"].astype(str).unique().tolist())

    def callees(self, symbol: str) -> List[str]:
        title = self.resolve(symbol)
        if not title:
            return []
        mask = (self.rels["source"].astype(str) == title) & (
            self.rels["type"].astype(str) == "calls"
        )
        return sorted(self.rels[mask]["target"].astype(str).unique().tolist())

    def neighbors(self, symbol: str) -> Dict[str, List[str]]:
        title = self.resolve(symbol)
        if not title:
            return {"incoming": [], "outgoing": []}
        inc = self.rels[(self.rels["target"].astype(str) == title)]["source"].astype(str).unique().tolist()
        out = self.rels[(self.rels["source"].astype(str) == title)]["target"].astype(str).unique().tolist()
        return {"incoming": sorted(inc), "outgoing": sorted(out)}

    def impact(self, symbol: str) -> List[str]:
        """Transitive callers (affected symbols)."""
        title = self.resolve(symbol)
        if not title:
            return []
        from collections import defaultdict, deque

        rev: Dict[str, List[str]] = defaultdict(list)
        call_mask = self.rels["type"].astype(str) == "calls"
        for _, row in self.rels[call_mask].astype(str).iterrows():
            rev[row["target"]].append(row["source"])

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

    def dependency_order(self) -> List[str]:
        """Topological-ish order based on contains (modules/files first)."""
        contains = self.rels[self.rels["type"].astype(str) == "contains"][["source", "target"]].astype(str)
        from collections import defaultdict, deque

        graph: Dict[str, List[str]] = defaultdict(list)
        indeg: Dict[str, int] = defaultdict(int)
        all_nodes = set(self.ents["title"].astype(str))

        for _, row in contains.iterrows():
            src, tgt = row["source"], row["target"]
            graph[src].append(tgt)
            indeg[tgt] += 1
            all_nodes.add(src)
            all_nodes.add(tgt)

        q = deque([n for n in all_nodes if indeg.get(n, 0) == 0])
        order: List[str] = []
        while q:
            n = q.popleft()
            order.append(n)
            for nei in graph[n]:
                indeg[nei] -= 1
                if indeg[nei] == 0:
                    q.append(nei)
        remaining = sorted(all_nodes - set(order))
        return order + remaining

    def symbol(self, query: str) -> Optional[Dict[str, Any]]:
        title = self.resolve(query)
        if not title or title not in self._title_to_row:
            return None
        row = self._title_to_row[title]
        snippet = row.get("snippet") if "snippet" in row else None
        return {
            "title": title,
            "type": row.get("type"),
            "description": row.get("description"),
            "source_file": row.get("source_file"),
            "span": row.get("span"),
            "snippet_preview": str(snippet)[:200] if snippet else None,
        }


# Back-compat helpers for existing code that expects dataframes
def load_byog(graph_dir: Path) -> Dict[str, pd.DataFrame]:
    g = ByogGraph(graph_dir)
    return {
        "entities": g.ents,
        "relationships": g.rels,
        "text_units": g.tus,
    }


def load_graph(graph_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    g = ByogGraph(graph_dir)
    return g.ents, g.rels
