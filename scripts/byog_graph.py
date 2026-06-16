"""
Common BYOG graph loader and model (extracted from context_pack and graph_query).

Provides a clean ByogGraph class that both tools can use.

This reduces duplication and makes it easier to add local queries, module packs, etc.

All deterministic, no external API.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
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


def _has_core_parquets(base: Path) -> bool:
    return (
        (base / "entities.parquet").exists()
        and (base / "relationships.parquet").exists()
        and (base / "text_units.parquet").exists()
    )


def _resolve_graph_base(root: Path) -> Path:
    """Resolve active parquet base from either root-level snapshots or output/ fallback."""
    root = Path(root)

    root_base = _resolve_output_base(root)
    if _has_core_parquets(root_base):
        return root_base

    out_base = root / "output"
    output_base = _resolve_output_base(out_base)
    if _has_core_parquets(output_base):
        return output_base

    # Keep the historical failure mode: let pandas raise a useful file-not-found
    # against output/ when neither layout exists.
    return output_base


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
    keep_last: int = 5,
    source_root: Optional[Path] = None,
    call_observations_df: Optional[pd.DataFrame] = None,
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
                    call_observations.parquet (if provided)
                    settings.yaml   (if provided)
                    manifest.json

    All writes inside the snapshot dir use per-file atomic .tmp + replace.
    The pointer 'current' is updated atomically with a tmp file + replace.

    This allows concurrent readers to always see a consistent previous snapshot
    while a new one is being built (e.g. by agent_port_loop).

    After publishing, automatically runs keep-last-N cleanup (default 5),
    always protecting the newly published current snapshot.
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

    has_obs = call_observations_df is not None and len(call_observations_df) > 0
    if has_obs:
        _atomic_write_parquet(call_observations_df, snap_dir / "call_observations.parquet")

    if settings_text:
        _atomic_write_text(settings_text, snap_dir / "settings.yaml")

    # manifest
    files_list = ["entities.parquet", "relationships.parquet", "text_units.parquet"]
    if has_obs:
        files_list.append("call_observations.parquet")
    manifest = {
        "id": snap_id,
        "created_at": datetime.now().isoformat(),
        "schema_version": 1,
        "counts": {
            "entities": len(entities_df),
            "relationships": len(relationships_df),
            "text_units": len(text_units_df),
            "call_observations": len(call_observations_df) if has_obs else 0,
        },
        "files": files_list,
        "source_root": str(source_root) if source_root else None,
        "git_commit": None,
        "total_size_bytes": None,
        "corpus_hash": None,
    }

    # Try to capture git commit (best effort)
    try:
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=out_root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        manifest["git_commit"] = git_commit
    except Exception:
        pass

    # Total size of the snapshot
    total_size = 0
    size_files = ["entities.parquet", "relationships.parquet", "text_units.parquet"]
    if has_obs:
        size_files.append("call_observations.parquet")
    for fname in size_files:
        f = snap_dir / fname
        if f.exists():
            total_size += f.stat().st_size
    manifest["total_size_bytes"] = total_size

    _atomic_write_text(json.dumps(manifest, indent=2), snap_dir / "manifest.json")

    # Atomically publish the pointer. Use a unique temp file so concurrent
    # snapshot writers do not race on a shared current.tmp path.
    _atomic_write_text(snap_id, out_root / "current")

    # Cleanup old snapshots (always protect current)
    cleanup_old_snapshots(out_root, keep_last=keep_last)

    return snap_dir


def cleanup_old_snapshots(out_root: Path, keep_last: int = 5) -> int:
    """Delete old snapshot directories, keeping at most the most recent `keep_last`.

    - Always reads `current` first and protects that snapshot (never deletes it).
    - `keep_last` is clamped to at least 1 because current must be retained.
    - Keeps current plus the newest remaining snapshots up to the total limit.
    - Snapshot dirs are sorted by name (timestamped names sort chronologically).
    - Only directories under snapshots/ are considered for deletion.
    - Returns the number of deleted snapshot directories.
    """
    out_root = Path(out_root)
    keep_last = max(1, keep_last)
    snapshots_dir = out_root / "snapshots"
    if not snapshots_dir.exists():
        return 0

    current_file = out_root / "current"
    current_id: Optional[str] = None
    if current_file.exists():
        try:
            current_id = current_file.read_text().strip()
        except Exception:
            current_id = None

    snap_dirs = [d for d in snapshots_dir.iterdir() if d.is_dir()]
    if not snap_dirs:
        return 0

    # Sort by name (YYYYMMDD-... format sorts correctly)
    snap_dirs.sort(key=lambda p: p.name)

    # Determine which to keep: current first, then newest remaining snapshots
    # until the total keep_last limit is reached.
    keep: set[Path] = set()
    if current_id:
        current_dir = snapshots_dir / current_id
        if current_dir.exists():
            keep.add(current_dir)

    slots_left = max(0, keep_last - len(keep))
    if slots_left > 0:
        candidates = [d for d in snap_dirs if d not in keep]
        keep.update(candidates[-slots_left:])

    deleted = 0
    for d in snap_dirs:
        if d not in keep:
            try:
                shutil.rmtree(d)
                deleted += 1
            except Exception:
                # Best effort; do not fail the whole operation
                pass

    return deleted


class ByogGraph:
    """Lightweight in-memory view over a BYOG (entities, relationships, text_units)."""

    def __init__(self, graph_dir: Path):
        self.root = Path(graph_dir)
        self._snap_base = _resolve_graph_base(self.root)
        self.ents: pd.DataFrame = pd.read_parquet(self._snap_base / "entities.parquet")
        self.rels: pd.DataFrame = pd.read_parquet(self._snap_base / "relationships.parquet")
        tus_path = self._snap_base / "text_units.parquet"
        self.tus: pd.DataFrame = (
            pd.read_parquet(tus_path) if tus_path.exists() else pd.DataFrame()
        )
        obs_path = self._snap_base / "call_observations.parquet"
        self.call_observations: pd.DataFrame = (
            pd.read_parquet(obs_path) if obs_path.exists() else pd.DataFrame()
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

    def observations(self, query: str) -> List[Dict[str, Any]]:
        """Return weak/ambiguous/container call observations for a symbol or module.

        This is a lightweight diagnostic for the resolver (annotation tracking,
        reassignment guards, builtin containers, ambiguous unions) without
        materializing a full context pack.
        """
        if len(self.call_observations) == 0:
            return []
        title = self.resolve(query)
        if title:
            ent = self.get_entity(title)
            is_module = ent is not None and str(ent.get("type", "")).lower() == "module"
            if is_module:
                module_prefix = title.split(":", 1)[0]
                mask = (
                    (self.call_observations["source"].astype(str) == title) |
                    (self.call_observations["source"].astype(str) == module_prefix) |
                    self.call_observations["source"].astype(str).str.startswith(module_prefix + ":")
                )
            else:
                mask = self.call_observations["source"].astype(str) == title
        else:
            # treat raw query as prefix (e.g. "sim" or "sim:run_simulation")
            mask = self.call_observations["source"].astype(str).str.startswith(query)
        if not mask.any():
            return []
        cols = [c for c in ["source", "display_target", "confidence", "reason", "source_file", "span"]
                if c in self.call_observations.columns]
        return self.call_observations.loc[mask, cols].to_dict(orient="records")


# Back-compat helpers for existing code that expects dataframes
def load_byog(graph_dir: Path) -> Dict[str, pd.DataFrame]:
    g = ByogGraph(graph_dir)
    res = {
        "entities": g.ents,
        "relationships": g.rels,
        "text_units": g.tus,
    }
    if len(g.call_observations) > 0:
        res["call_observations"] = g.call_observations
    return res


def load_graph(graph_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    g = ByogGraph(graph_dir)
    return g.ents, g.rels
