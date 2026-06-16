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
import sys
import typer

# Support both `python -m scripts.xxx` and direct `python scripts/xxx.py`
sys.path.insert(0, str(Path(__file__).parent))
from byog_graph import load_byog  # common loader

app = typer.Typer(help="Assemble a context pack for a code symbol from a BYOG graph (entities/rels/tus parquets).")


def find_entity(ents: pd.DataFrame, symbol: str) -> pd.Series | None:
    """Exact match preferred. On ambiguous partial matches, error with candidates list."""
    titles = ents["title"].astype(str)
    types = ents["type"].astype(str).str.lower() if "type" in ents.columns else pd.Series([], dtype=str)

    exact = ents[ents["title"].astype(str) == symbol]
    if len(exact) == 1:
        return exact.iloc[0]
    if len(exact) > 1:
        cands = list(exact["title"].astype(str))
        typer.secho(f"Multiple exact matches for '{symbol}': {cands}", fg=typer.colors.RED)
        return None

    # Bare module aliases: "sim" should resolve to the module entity "sim:sim",
    # not become ambiguous with sim.py and all sim:* symbols.
    if len(types) == len(ents):
        module_alias = ents[
            (types == "module")
            & (
                (titles == symbol)
                | (titles == f"{symbol}:{symbol}")
                | titles.str.endswith(":" + symbol)
            )
        ]
        if len(module_alias) == 1:
            return module_alias.iloc[0]
        if len(module_alias) > 1:
            cands = list(module_alias["title"].astype(str))
            typer.secho(f"Ambiguous module alias '{symbol}'. Candidates: {cands}", fg=typer.colors.YELLOW)
            return None

    partial = ents[titles.str.contains(symbol, case=False, na=False)]
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


def compact_relationship(rel: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": rel.get("id"),
        "source": rel.get("source"),
        "target": rel.get("target"),
        "type": rel.get("type"),
        "description": rel.get("description"),
        "weight": rel.get("weight"),
    }


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

    # Build base pack first
    neighbors = get_neighbors(rels, str(ent_dict.get("id", "")), str(ent_dict.get("title", "")))
    texts = get_text_units(tus, ent, neighbors)

    pack: Dict[str, Any] = {
        "symbol": ent_dict.get("title"),
        "purpose": purpose,
        "entity": {
            k: v for k, v in ent_dict.items()
            if k in ("id", "title", "type", "description", "source_file", "span", "extractor", "confidence", "is_deterministic")
        },
        "neighbors": [compact_relationship(nr) for nr in neighbors[:30]],
    }

    # Auto-detect module/subsystem pack
    is_module_pack = str(ent_dict.get("type", "")).lower() == "module"
    if is_module_pack:
        module_title = str(ent_dict.get("title"))
        module_stem = module_title.split(":", 1)[0] if ":" in module_title else module_title
        module_prefix = module_stem + ":"
        entity_titles = ents["title"].astype(str)
        members_mask = entity_titles.str.startswith(module_prefix)
        members = ents[members_mask][["title", "type", "description"]].to_dict(orient="records")
        member_titles = set(ents[members_mask]["title"].astype(str))
        rel_mask = rels["source"].astype(str).isin(member_titles) | rels["target"].astype(str).isin(member_titles)
        module_relationships = rels[rel_mask].to_dict(orient="records")
        wanted_text_units = set()
        for _, member in ents[members_mask].iterrows():
            wanted_text_units.update(str(x) for x in _to_list(member.get("text_unit_ids")))
        for rel in module_relationships:
            wanted_text_units.update(str(x) for x in _to_list(rel.get("text_unit_ids")))
        if wanted_text_units and len(tus) > 0:
            text_mask = tus["id"].astype(str).isin(wanted_text_units)
            texts = tus[text_mask].to_dict(orient="records")
        pack["is_module_pack"] = True
        pack["module_prefix"] = module_prefix
        pack["members"] = members[:50]
        pack["module_neighbors"] = [compact_relationship(r) for r in module_relationships[:100]]

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

    # Augment the pack we built earlier
    pack.update({
        "text_units": packed_texts,
        "provenance": {
            "source_file": ent_dict.get("source_file"),
            "span": ent_dict.get("span"),
            "extractor": ent_dict.get("extractor"),
            "confidence": ent_dict.get("confidence"),
            "is_deterministic": ent_dict.get("is_deterministic"),
        },
        "golden_contract_note": golden_note if golden_note else None,
        "behavior_contract": "examples/mini_game/tests/behavior_contract.json (load for machine-readable invariants and expected values per scenario)" if "mini_game" in str(graph) else None,
        "usage_hint": "Use this pack + the original source of the listed files when prompting an LLM to port the symbol to Rust while preserving exact observable behavior on the golden inputs.",
        "truncation": {
            "max_text_chars": max_text_chars if max_text_chars > 0 else None,
            "full_text": full_text or max_text_chars <= 0,
        },
    })

    # Surface first-class weak/ambiguous call observations (from call_observations.parquet)
    # so the porting agent can see uncertain call sites with honest confidence tiers.
    obs = data.get("call_observations")
    if obs is not None and len(obs) > 0:
        symbol_title = str(ent_dict.get("title", ""))
        # Match on source title (exact or module prefix for members)
        try:
            obs_src = obs["source"].astype(str)
            mask = (obs_src == symbol_title) | obs_src.str.startswith(symbol_title + ":") | obs_src.str.contains(symbol_title.split(":")[-1], case=False, na=False)
            relevant = obs[mask].head(15)
            if len(relevant) > 0:
                uncertain = []
                for _, o in relevant.iterrows():
                    uncertain.append({
                        "source": str(o.get("source", "")),
                        "display_target": str(o.get("display_target", "")),
                        "confidence": float(o.get("confidence", 0.0)),
                        "reason": str(o.get("reason", "")),
                        "provenance": f"{o.get('source_file', '')}:{o.get('span', '')}",
                    })
                pack["uncertain_calls"] = uncertain
                pack["analysis_note"] = "Some call sites were tracked with low confidence or ambiguity (see uncertain_calls). Review during port."
        except Exception:
            pass  # best-effort; observations are supplemental

    result = json.dumps(pack, indent=2, ensure_ascii=False)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(result)
        typer.echo(f"Wrote context pack to {output}")
    else:
        typer.echo(result)


if __name__ == "__main__":
    app()
