#!/usr/bin/env python
"""
context-pack CLI skeleton (for point 3 of the plan).

Given a symbol title (e.g. "sim:run_simulation"), loads the BYOG parquets
and assembles a rich context pack:
  - the entity itself + provenance
  - direct neighbors (relationships in both directions)
  - associated text units
  - for mini_game symbols: reference to the golden behavior contract / collision tests

Designed to be pasted into an LLM prompt for "port-to-rust".

Usage:
    uv run python scripts/context_pack.py sim:run_simulation --graph byog_mini_game --purpose port-to-rust

Later this can be backed by GraphRAG Local/Global search or a proper graph query engine.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd
import typer

app = typer.Typer(help="Assemble a context pack for a code symbol from a BYOG graph (entities/rels/tus parquets).")


def load_byog(graph_dir: Path) -> Dict[str, pd.DataFrame]:
    out = graph_dir / "output"
    return {
        "entities": pd.read_parquet(out / "entities.parquet"),
        "relationships": pd.read_parquet(out / "relationships.parquet"),
        "text_units": pd.read_parquet(out / "text_units.parquet") if (out / "text_units.parquet").exists() else pd.DataFrame(),
    }


def find_entity(ents: pd.DataFrame, symbol: str) -> pd.Series | None:
    """Exact match preferred. On ambiguous partial matches, error with candidates list."""
    exact = ents[ents["title"].astype(str) == symbol]
    if len(exact) == 1:
        return exact.iloc[0]
    if len(exact) > 1:
        cands = list(exact["title"].astype(str))
        typer.secho(f"Multiple exact matches for '{symbol}': {cands}", fg=typer.colors.RED)
        return None

    partial = ents[ents["title"].astype(str).str.contains(symbol, case=False, na=False)]
    if len(partial) == 0:
        return None
    if len(partial) > 1:
        cands = list(partial["title"].astype(str))
        typer.secho(
            f"Ambiguous symbol '{symbol}'. Candidates: {cands}. "
            "Use a more precise title (e.g. 'sim:run_simulation' or 'core:Config').",
            fg=typer.colors.YELLOW,
        )
        return None
    return partial.iloc[0]


def get_neighbors(rels: pd.DataFrame, entity_id: str, entity_title: str) -> List[Dict[str, Any]]:
    mask = (rels["source"].astype(str) == entity_title) | (rels["target"].astype(str) == entity_title) | \
           (rels["source"].astype(str) == entity_id) | (rels["target"].astype(str) == entity_id)
    return rels[mask].to_dict(orient="records")


def _to_list(val: Any) -> List[Any]:
    if val is None:
        return []
    if hasattr(val, "tolist"):
        val = val.tolist()
    if isinstance(val, (list, tuple)):
        return list(val)
    if isinstance(val, (set, pd.Series)):
        return list(val)
    return [val]


def get_text_units(tus: pd.DataFrame, entity_row: pd.Series, neighbor_rels: List[Dict]) -> List[Dict[str, Any]]:
    wanted = set(str(x) for x in _to_list(entity_row.get("text_unit_ids")))
    for r in neighbor_rels:
        wanted.update(str(x) for x in _to_list(r.get("text_unit_ids")))
    if not wanted or len(tus) == 0:
        return []
    mask = tus["id"].astype(str).isin(list(wanted))
    return tus[mask].to_dict(orient="records") if mask.any() else []


@app.command()
def pack(
    symbol: str = typer.Argument(..., help="Symbol title or partial, e.g. sim:run_simulation or update_player"),
    graph: Path = typer.Option(Path("byog_mini_game"), "--graph", "-g", help="Directory containing the BYOG (with output/ subdir)"),
    purpose: str = typer.Option("port-to-rust", "--purpose", "-p"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write JSON to this path instead of stdout"),
    max_text_chars: int = typer.Option(300, "--max-text-chars", help="Truncate text units to this many chars (0 or negative = no limit)"),
    full_text: bool = typer.Option(False, "--full-text", help="Equivalent to --max-text-chars 0 (no truncation)"),
):
    """Assemble and print (or save) a context pack for the given symbol."""
    if not (graph / "output" / "entities.parquet").exists():
        typer.secho(f"BYOG not found under {graph}. Run the bridge or smoke generator first.", fg=typer.colors.RED)
        raise typer.Exit(1)

    if full_text:
        max_text_chars = 0

    data = load_byog(graph)
    ents, rels, tus = data["entities"], data["relationships"], data["text_units"]

    ent = find_entity(ents, symbol)
    if ent is None:
        typer.secho(f"No entity found for {symbol}", fg=typer.colors.RED)
        # show some available titles for help
        print("Available (sample):", list(ents["title"].astype(str).head(8)))
        raise typer.Exit(2)

    ent_dict = ent.to_dict()
    neighbors = get_neighbors(rels, str(ent_dict.get("id", "")), str(ent_dict.get("title", "")))
    texts = get_text_units(tus, ent, neighbors)

    # Golden contract note for mini_game symbols
    golden_note = ""
    if "mini_game" in str(graph) or "sim" in str(ent_dict.get("title", "")) or "physics" in str(ent_dict.get("title", "")):
        golden_note = (
            "GOLDEN BEHAVIOR CONTRACT: The mini_game simulator has committed golden traces (see examples/mini_game/tests/golden_*.json). "
            "All ports must pass the exact same state/collided/score traces for the defined input sequences, including the collision_first scenario (jumps=[6] produces collided=True at tick 9). "
            "See test_collision_first and test_golden_trace_matches."
        )

    def truncate_text(txt: str, limit: int) -> tuple[str, bool]:
        if limit <= 0 or len(txt) <= limit:
            return txt, False
        return txt[:limit], True

    packed_texts = []
    for t in texts[:10]:
        raw = str(t.get("text", ""))
        truncated_text, was_truncated = truncate_text(raw, max_text_chars)
        packed_texts.append({
            "id": t.get("id"),
            "text": truncated_text,
            "truncated": was_truncated,
        })

    pack: Dict[str, Any] = {
        "symbol": ent_dict.get("title"),
        "purpose": purpose,
        "entity": {
            k: v for k, v in ent_dict.items()
            if k in ("id", "title", "type", "description", "source_file", "span", "extractor", "confidence", "is_deterministic")
        },
        "neighbors": [
            {
                "id": nr.get("id"),
                "source": nr.get("source"),
                "target": nr.get("target"),
                "type": nr.get("type"),
                "description": nr.get("description"),
                "weight": nr.get("weight"),
            }
            for nr in neighbors[:30]  # cap for prompt size
        ],
        "text_units": packed_texts,
        "provenance": {
            "source_file": ent_dict.get("source_file"),
            "span": ent_dict.get("span"),
            "extractor": ent_dict.get("extractor"),
            "confidence": ent_dict.get("confidence"),
            "is_deterministic": ent_dict.get("is_deterministic"),
        },
        "golden_contract_note": golden_note if golden_note else None,
        "usage_hint": "Use this pack + the original source of the listed files when prompting an LLM to port the symbol to Rust while preserving exact observable behavior on the golden inputs.",
        "truncation": {
            "max_text_chars": max_text_chars if max_text_chars > 0 else None,
            "full_text": full_text or max_text_chars <= 0,
        },
    }

    result = json.dumps(pack, indent=2, ensure_ascii=False)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result)
        typer.echo(f"Wrote context pack to {output}")
    else:
        typer.echo(result)


if __name__ == "__main__":
    app()
