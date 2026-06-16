"""
Common BYOG graph loader and model (extracted from context_pack and graph_query).

Provides a clean ByogGraph class that both tools can use.

This reduces duplication and makes it easier to add local queries, module packs, etc.

All deterministic, no external API.
"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def _resolve_output_base(base: Path) -> Path:
    """Return the directory containing the active parquets.

    Supports new snapshot layout:
        <base>/
            current          # contains snapshot id
            snapshots/
                <id>/
                    entities.parquet
                    ...
    Falls back to flat structure (old behavior or test tmp dirs) if no 'current' or the snapshot dir is missing.
    """
    base = Path(base)
    current_file = base / "current"
    if current_file.exists():
        try:
            snap_id = current_file.read_text().strip()
            snap_dir = base / "snapshots" / snap_id
            if snap_dir.exists():
                return snap_dir
        except Exception:
            pass
    # flat fallback (direct writes in tests, old byog dirs, etc.)
    return base


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


def publish_byog_snapshot(
    entities_df: pd.DataFrame,
    relationships_df: pd.DataFrame,
    text_units_df: pd.DataFrame,
    out_root: Path,
    settings_text: str | None = None,
) -> Path:
    """Write a complete BYOG snapshot atomically and publish a 'current' pointer.

    Layout created:
        out_root/
            current                 # text file with the snapshot id
            snapshots/
                <id>/
                    entities.parquet
                    relationships.parquet
                    text_units.parquet
                    settings.yaml   (if provided)
                    manifest.json

    All writes inside the snapshot dir use per-file atomic .tmp + replace.
    The pointer 'current' is updated atomically with a tmp file + replace.

    This allows concurrent readers to always see a consistent previous snapshot
    while a new one is being built (e.g. by agent_port_loop).
    """
    out_root = Path(out_root)
    snapshots_dir = out_root / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    snap_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    snap_dir = snapshots_dir / snap_id
    snap_dir.mkdir(parents=True, exist_ok=True)

    # Write files atomically inside the new snapshot dir
    _atomic_write_parquet(entities_df, snap_dir / "entities.parquet")
    _atomic_write_parquet(relationships_df, snap_dir / "relationships.parquet")
    _atomic_write_parquet(text_units_df, snap_dir / "text_units.parquet")

    if settings_text:
        _atomic_write_text(settings_text, snap_dir / "settings.yaml")

    # manifest
    manifest = {
        "id": snap_id,
        "created": datetime.now().isoformat(),
        "files": ["entities.parquet", "relationships.parquet", "text_units.parquet"],
    }
    _atomic_write_text(json.dumps(manifest, indent=2), snap_dir / "manifest.json")

    # Atomically publish the pointer
    current_tmp = out_root / "current.tmp"
    current_tmp.write_text(snap_id)
    os.replace(current_tmp, out_root / "current")

    return snap_dir


class ByogGraph:
    """Lightweight in-memory view over a BYOG (entities, relationships, text_units)."""

    def __init__(self, graph_dir: Path):
        self.root = Path(graph_dir)
        out_base = self.root / "output"
        self._snap_base = _resolve_output_base(out_base)
        self.ents: pd.DataFrame = pd.read_parquet(self._snap_base / "entities.parquet")
        self.rels: pd.DataFrame = pd.read_parquet(self._snap_base / "relationships.parquet")
        tus_path = self._snap_base / "text_units.parquet"
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
