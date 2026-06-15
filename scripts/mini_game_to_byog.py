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
    uv run python scripts/mini_game_to_byog.py
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

# Make extractor importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from extract_python import extract_from_file  # type: ignore

PACKAGE_DIR = ROOT / "examples" / "mini_game"
OUT_ROOT = ROOT / "byog_mini_game"
OUT_DIR = OUT_ROOT / "output"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def build_byog_for_package() -> Dict[str, List[Dict[str, Any]]]:
    all_entities: List[Dict[str, Any]] = []
    all_relationships: List[Dict[str, Any]] = []
    all_text_units: List[Dict[str, Any]] = []

    py_files = sorted(
        p for p in PACKAGE_DIR.rglob("*.py")
        if "tests" not in p.parts and p.name != "__init__.py"
    )

    human_id = 0
    tu_id = 0
    rel_id = 0

    title_to_id: Dict[str, str] = {}  # title -> entity id (for possible future)
    title_to_text_unit: Dict[str, str] = {}

    def slug(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.:-]+", "_", value).strip("_")

    for py_file in py_files:
        rel = extract_from_file(py_file)
        stem = py_file.stem

        for e in rel["entities"]:
            human_id += 1
            # Make title stable and FQN-ish for cross-file uniqueness and matching
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
            # Keep provenance
            title_to_id[fqn_title] = e["id"]
            title_to_text_unit[fqn_title] = symbol_tu_id
            all_entities.append(e)

            # text unit per entity for simplicity (or per file)
            tu_id += 1
            all_text_units.append({
                "id": symbol_tu_id,
                "human_readable_id": tu_id,
                "text": e.get("description", "") or f"symbol {fqn_title} from {py_file}",
                "n_tokens": max(1, len((e.get("description", "") or "").split())),
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

        for r in rel["relationships"]:
            rel_id += 1
            r["id"] = f"rel:{stem}:{rel_id}"
            r["human_readable_id"] = rel_id
            if "document_ids" not in r:
                r["document_ids"] = [f"doc:{stem}"]
            if "covariate_ids" not in r:
                r["covariate_ids"] = []

            # CRITICAL FIX: source/target must be titles that exist in entities.title
            # (not the internal ent: ids). Use the (now FQN) titles.
            # The extractor used simple names or ent:; we override here to titles.
            # For contains we can map, for calls too.
            # Since we just set titles above, we can re-resolve using original intent.
            # For this bridge we reconstruct from the extracted data using titles.
            src_title = r.get("source", "")
            tgt_title = r.get("target", "")

            # CRITICAL FIX for GraphRAG 3.1 create_communities:
            # source/target MUST match values in entities["title"] (we use FQN titles).
            # Drop crude/unknown ones (e.g. import text turned into bad targets) to avoid dangling.
            def resolve_to_title(raw: str) -> str:
                if not raw:
                    return ""
                if raw in title_to_id:
                    return raw
                last = raw.split(":")[-1].strip()
                for t in list(title_to_id.keys()):
                    if t == last or t.endswith(":" + last):
                        return t
                return raw

            src_res = resolve_to_title(src_title)
            tgt_res = resolve_to_title(tgt_title)
            r["source"] = src_res
            r["target"] = tgt_res

            if src_res and tgt_res and src_res in title_to_id and tgt_res in title_to_id:
                text_unit_ids = []
                for title in (src_res, tgt_res):
                    tu_ref = title_to_text_unit.get(title)
                    if tu_ref and tu_ref not in text_unit_ids:
                        text_unit_ids.append(tu_ref)
                r["text_unit_ids"] = text_unit_ids
                all_relationships.append(r)
            # else dropped (imports etc can be improved in Phase 1 with real module entities)

    # Dedup text_units rough
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
    }


def main() -> None:
    data = build_byog_for_package()

    ents_df = pd.DataFrame(data["entities"])
    rels_df = pd.DataFrame(data["relationships"])
    tus_df = pd.DataFrame(data["text_units"])

    # Ensure required columns exist (add empties if missing for full compatibility)
    for df in (ents_df, rels_df, tus_df):
        for col in ("document_ids", "covariate_ids"):
            if col not in df.columns:
                df[col] = [[] for _ in range(len(df))]

    pq.write_table(pa.Table.from_pandas(ents_df), OUT_DIR / "entities.parquet")
    pq.write_table(pa.Table.from_pandas(rels_df), OUT_DIR / "relationships.parquet")
    pq.write_table(pa.Table.from_pandas(tus_df), OUT_DIR / "text_units.parquet")

    # Minimal settings for later
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
    (OUT_ROOT / "settings.yaml").write_text(settings)

    print(f"Bridge complete. Entities: {len(ents_df)}, Relationships: {len(rels_df)}, TextUnits: {len(tus_df)}")
    print(f"Parquets written to {OUT_DIR}")
    print("Titles used for rel endpoints (sample):")
    if len(rels_df):
        print("  ", rels_df.iloc[0][["source", "target"]].to_dict())


if __name__ == "__main__":
    main()
