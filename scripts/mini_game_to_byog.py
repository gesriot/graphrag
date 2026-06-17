#!/usr/bin/env python
"""
extractor -> BYOG bridge for the entire examples/mini_game package.

Walks all relevant .py files (excluding tests), extracts with tree-sitter,
normalizes to stable titles (FQN-ish: stem:name), ensures relationships.source/target
use the *titles* (not internal ent: ids) so that GraphRAG create_communities can
resolve them correctly against entities.title.

Outputs the three canonical parquets + a settings stub.

This is the "Phase 1 bridge" to give a solid rail before spending LLM tokens.

Run:
    uv run python scripts/mini_game_to_byog.py --keep-snapshots 5 [--use-advanced]
Then (with key later):
    uv run graphrag index --root byog_mini_game
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import typer

import os
import tempfile

# Make extractor importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from extract_python import extract_from_file  # type: ignore
from byog_graph import publish_byog_snapshot  # full snapshot atomic publish + current pointer

PACKAGE_DIR = ROOT / "examples" / "mini_game"
OUT_ROOT = ROOT / "byog_mini_game"
OUT_DIR = OUT_ROOT / "output"

app = typer.Typer(help="Generate BYOG snapshot for the mini_game package (atomic writes + retention).")


def _atomic_write_parquet(df: pd.DataFrame, final_path: Path) -> None:
    """Write a single parquet atomically (tmp file in same dir + os.replace).

    Prevents graph readers (context_pack, graph_query, tests, agent readers)
    from seeing a half-written parquet while the agent_loop is regenerating.
    """
    final_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=final_path.parent, suffix=".parquet.tmp", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        table = pa.Table.from_pandas(df)
        pq.write_table(table, tmp_path)
        os.replace(tmp_path, final_path)  # atomic on POSIX and modern Windows
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _atomic_write_text(text: str, final_path: Path) -> None:
    """Atomic write for text files such as settings.yaml."""
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


def build_byog_for_package(use_advanced: bool = False, package_dir: Path | None = None) -> Dict[str, List[Dict[str, Any]]]:
    """Two-pass bridge (P1 fix).

    Pass 1: collect *all* entities and titles across every file first.
    Pass 2: resolve and emit relationships using the complete title map.
    This prevents dropping cross-file calls (e.g. main.py -> sim.run_simulation)
    that were previously filtered before later files contributed their titles.
    """
    pkg_dir = package_dir or PACKAGE_DIR
    all_entities: List[Dict[str, Any]] = []
    all_relationships: List[Dict[str, Any]] = []
    all_text_units: List[Dict[str, Any]] = []

    py_files = sorted(
        p for p in pkg_dir.rglob("*.py")
        if "tests" not in p.parts and p.name != "__init__.py"
    )

    human_id = 0
    tu_id = 0
    rel_id = 0

    title_to_id: Dict[str, str] = {}
    id_to_title: Dict[str, str] = {}
    title_to_text_unit: Dict[str, str] = {}

    # Raw data collected in pass 1
    per_file_raw: List[Dict[str, Any]] = []  # one entry per py_file

    def slug(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.:-]+", "_", value).strip("_")

    def module_key(py_file: Path) -> str:
        """Stable dotted module key relative to the indexed package root."""
        try:
            rel = py_file.relative_to(pkg_dir)
        except ValueError:
            rel = py_file.name
        without_suffix = Path(rel).with_suffix("")
        return ".".join(without_suffix.parts)

    # ===================== PASS 1: collect entities + titles =====================
    for py_file in py_files:
        rel = extract_from_file(py_file, use_advanced=use_advanced)
        stem = module_key(py_file)

        file_entities = []
        for e in rel["entities"]:
            human_id += 1
            original_id = e["id"]
            original_title = e["title"]
            fqn_title = f"{stem}:{original_title}" if stem != "mini_game" else original_title
            symbol_tu_id = f"tu:{stem}:{slug(original_title)}"
            e["title"] = fqn_title
            e["id"] = f"ent:{e['type']}:{fqn_title}"
            e["text_unit_ids"] = [symbol_tu_id]
            e["human_readable_id"] = human_id
            if "document_ids" not in e:
                e["document_ids"] = [f"doc:{stem}"]
            if "covariate_ids" not in e:
                e["covariate_ids"] = []
            id_to_title[original_id] = fqn_title
            if e["type"] == "method" and original_id.startswith("ent:method:"):
                # Older extractor rows used ent:fn for some method call sources.
                # Keep observations/context packs human-readable across both forms.
                id_to_title[original_id.replace("ent:method:", "ent:fn:", 1)] = fqn_title
            title_to_id[fqn_title] = e["id"]
            id_to_title[e["id"]] = fqn_title
            title_to_text_unit[fqn_title] = symbol_tu_id
            all_entities.append(e)
            file_entities.append(e)

            tu_id += 1
            snippet = e.get("snippet", "") or ""
            desc = e.get("description", "") or ""
            text = snippet if snippet else (desc or f"symbol {fqn_title} from {py_file}")
            all_text_units.append({
                "id": symbol_tu_id,
                "human_readable_id": tu_id,
                "text": text,
                "n_tokens": max(1, len(text.split())),
                "document_id": f"doc:{stem}",
                "document_ids": e.get("document_ids", []),
                "entity_ids": [e["id"]],
                "relationship_ids": [],
                "covariate_ids": [],
                "source_file": e.get("source_file"),
                "span": e.get("span"),
                "extractor": e.get("extractor"),
                "confidence": e.get("confidence"),
                "is_deterministic": e.get("is_deterministic"),
            })

        per_file_raw.append({
            "stem": stem,
            "py_file": py_file,
            "relationships": rel.get("relationships", []),
            "imports": rel.get("imports", []),
        })

    # ===================== PASS 2: resolve relationships with complete map =====================
    call_observations: List[Dict[str, Any]] = []

    for raw in per_file_raw:
        stem = raw["stem"]
        py_file = raw["py_file"]
        for r in raw["relationships"]:
            rel_id += 1
            r["id"] = f"rel:{stem}:{rel_id}"
            r["human_readable_id"] = rel_id
            if "document_ids" not in r:
                r["document_ids"] = [f"doc:{stem}"]
            if "covariate_ids" not in r:
                r["covariate_ids"] = []

            src_title = r.get("source", "")
            tgt_title = r.get("resolved_target_hint") or r.get("target", "")
            raw_target = r.get("target", "")

            def observation_display_target(rel: Dict[str, Any]) -> str:
                desc = str(rel.get("description", ""))
                marker = "ast Attribute: "
                if marker in desc:
                    return desc.split(marker, 1)[1].split(" -> ", 1)[0].strip()
                return str(rel.get("resolved_target_hint") or tgt_title or raw_target)

            def resolve_to_title(raw: str) -> str:
                if not raw:
                    return ""
                if raw in title_to_id:
                    return raw
                if raw in id_to_title:
                    return id_to_title[raw]
                # Internal extractor ids are file-stem based (e.g. ent:fn:index_python:main).
                # Resolve them in the context of the current module key instead of falling
                # back to a global bare-name match, which can leak calls across same-named
                # functions such as multiple main() definitions.
                if raw.startswith("ent:"):
                    parts = raw.split(":")
                    if len(parts) >= 3:
                        if parts[1] == "file":
                            symbol = ":".join(parts[2:])
                        elif len(parts) >= 4:
                            symbol = ":".join(parts[3:])
                        else:
                            symbol = parts[-1]
                        for candidate in (
                            f"{stem}:{symbol}",
                            f"{stem}:{symbol.replace('_', '.')}",
                        ):
                            if candidate in title_to_id:
                                return candidate
                if ":" in raw:
                    _, symbol = raw.split(":", 1)
                    contextual = f"{stem}:{symbol}"
                    if contextual in title_to_id:
                        return contextual
                last = raw.split(":")[-1].strip()
                for t in list(title_to_id.keys()):
                    if t == last or t.endswith(":" + last):
                        return t
                return raw

            src_res = resolve_to_title(src_title)
            tgt_res = resolve_to_title(tgt_title)
            r["source"] = src_res
            r["target"] = tgt_res

            # Preserve ambiguous/weak calls (guarded reassigns, branch ctor ambiguity, low-conf)
            # as first-class observations so they are not lost in the core relationships filter.
            # Core relationships stay clean (high-quality resolved only). Observations carry
            # source, display_target, confidence, reason, provenance for context_pack / agents.
            drop_from_core = False
            if r.get("type") == "calls" and "tree-sitter-python+ast" in str(r.get("extractor", "")):
                orig_desc = str(r.get("description", ""))
                conf = float(r.get("confidence", 0.0) or 0.0)
                has_good_hint = bool(r.get("resolved_target_hint"))
                reason = ""
                if "builtin container" in orig_desc:
                    reason = "builtin/container call observation"
                elif "ambiguous annotation" in orig_desc:
                    reason = "ambiguous annotation"
                elif "ambiguous constructors" in orig_desc:
                    reason = "ambiguous constructors"
                elif "guarded by reassignment" in orig_desc:
                    reason = "guarded by reassignment"
                elif "unresolved receiver" in orig_desc:
                    reason = "unresolved receiver"
                elif conf < 0.6:
                    reason = "low confidence"
                if reason or conf < 0.7 or not has_good_hint:
                    obs = {
                        "source": src_res or src_title,
                        "display_target": observation_display_target(r),
                        "confidence": conf,
                        "reason": reason or "low confidence call observation",
                        "source_file": r.get("source_file", ""),
                        "span": r.get("span", ""),
                        "extractor": r.get("extractor", ""),
                        "description": orig_desc,
                    }
                    call_observations.append(obs)
                    drop_from_core = conf < 0.7 or not has_good_hint

            if drop_from_core:
                continue

            if src_res and tgt_res and src_res in title_to_id and tgt_res in title_to_id:
                text_unit_ids = []
                for title in (src_res, tgt_res):
                    tu_ref = title_to_text_unit.get(title)
                    if tu_ref and tu_ref not in text_unit_ids:
                        text_unit_ids.append(tu_ref)
                r["text_unit_ids"] = text_unit_ids
                all_relationships.append(r)
            # else: dropped from core relationships (ambiguous/weak go to observations instead)

    # Cross-file call resolution (now safe because title_to_id is complete)
    bare_to_fqns: Dict[str, List[str]] = {}
    for t in title_to_id:
        bare = t.split(":")[-1]
        bare_to_fqns.setdefault(bare, []).append(t)

    upgraded_calls = []
    for r in all_relationships:
        if r.get("type") != "calls":
            upgraded_calls.append(r)
            continue
        tgt = str(r.get("target", ""))
        if ":" not in tgt:
            candidates = bare_to_fqns.get(tgt, [])
            if len(candidates) == 1:
                r["target"] = candidates[0]
                r["description"] = r.get("description", "") + " (cross-file resolved)"
                r["confidence"] = 0.75
                r["is_deterministic"] = False
        upgraded_calls.append(r)
    all_relationships = upgraded_calls

    # Dedup text_units
    seen_tu = {}
    for tu in all_text_units:
        seen_tu[tu["id"]] = tu
    all_text_units = list(seen_tu.values())

    relationship_ids_by_tu: Dict[str, List[str]] = {}
    for r in all_relationships:
        for tuid in r.get("text_unit_ids", []):
            relationship_ids_by_tu.setdefault(tuid, []).append(r["id"])
    for tu in all_text_units:
        tu["relationship_ids"] = relationship_ids_by_tu.get(tu["id"], [])

    return {
        "entities": all_entities,
        "relationships": all_relationships,
        "text_units": all_text_units,
        "call_observations": call_observations,
    }


@app.command()
def main(
    keep_snapshots: int = typer.Option(
        5,
        "--keep-snapshots",
        "--keep-last",
        help="Maximum number of snapshots to retain (current is always protected and counts toward the limit).",
    ),
    use_advanced: bool = typer.Option(
        False,
        "--use-advanced",
        "--use-jedi-pyright",
        help="Try optional Jedi/Pyright for richer name resolution (still local, higher confidence tier). Falls back gracefully.",
    ),
) -> None:
    data = build_byog_for_package(use_advanced=use_advanced, package_dir=PACKAGE_DIR)

    ents_df = pd.DataFrame(data["entities"])
    rels_df = pd.DataFrame(data["relationships"])
    tus_df = pd.DataFrame(data["text_units"])
    obs_df = pd.DataFrame(data.get("call_observations", []))

    # Ensure required columns exist (add empties if missing for full compatibility)
    for df in (ents_df, rels_df, tus_df):
        for col in ("document_ids", "covariate_ids"):
            if col not in df.columns:
                df[col] = [[] for _ in range(len(df))]

    # Snapshot-level atomic publish.
    # All three parquets + settings are written inside a fresh snapshots/<id>/
    # using per-file atomic writes, then the "current" pointer is updated
    # with a single atomic replace. Readers always see a consistent previous
    # snapshot (no version skew between entities/relationships/text_units).
    # call_observations.parquet (if any) is also published to preserve weak/ambiguous
    # analysis signals without polluting the core resolved relationships.
    settings = """\
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
    snap_dir = publish_byog_snapshot(
        ents_df, rels_df, tus_df, OUT_ROOT, settings,
        keep_last=keep_snapshots,
        source_root=PACKAGE_DIR,
        call_observations_df=obs_df if len(obs_df) > 0 else None,
    )

    print(f"Bridge complete. Entities: {len(ents_df)}, Relationships: {len(rels_df)}, TextUnits: {len(tus_df)}")
    print(f"Snapshot published under {snap_dir}")
    print("Current pointer updated atomically.")
    print("Titles used for rel endpoints (sample):")
    if len(rels_df):
        print("  ", rels_df.iloc[0][["source", "target"]].to_dict())


if __name__ == "__main__":
    app()
