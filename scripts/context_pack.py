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
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

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
                | (titles == f"{symbol}:__module__")
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


def _json_safe(val: Any) -> Any:
    """Convert numpy/pandas values to plain Python for JSON serialization."""
    if val is None:
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, (list, tuple)):
        return [_json_safe(x) for x in val]
    if isinstance(val, dict):
        return {str(k): _json_safe(v) for k, v in val.items()}
    # numpy ndarray / pandas extension arrays
    if hasattr(val, "tolist") and not isinstance(val, (bytes, memoryview)):
        try:
            return _json_safe(val.tolist())
        except Exception:
            pass
    # numpy scalar (bool_, int64, …)
    if hasattr(val, "item"):
        try:
            return _json_safe(val.item())
        except Exception:
            pass
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    return val


def _as_bool(val: Any) -> bool:
    v = _json_safe(val)
    if v is None:
        return False
    return bool(v)


def _reasons_list(val: Any) -> List[str]:
    return [str(x) for x in _to_list(val) if x is not None and str(x) not in ("", "nan", "None")]


def _type_json_safe(val: Any) -> Any:
    """JSON normalization local to configured type evidence."""
    if val is None:
        return None
    if isinstance(val, float) and not math.isfinite(val):
        return None
    if isinstance(val, (str, int, float, bool)):
        return val
    if isinstance(val, (list, tuple)):
        return [_type_json_safe(item) for item in val]
    if isinstance(val, dict):
        return {
            str(key): _type_json_safe(value) for key, value in val.items()
        }
    normalized = _json_safe(val)
    if normalized is val:
        return normalized
    return _type_json_safe(normalized)


def _material_json_value(val: Any) -> Any:
    """Return a JSON-safe material value, treating parquet nulls as absent."""
    normalized = _type_json_safe(val)
    if normalized is None or normalized == "" or normalized == "nan":
        return None
    return normalized


def _first_material_json_value(*values: Any) -> Any:
    for value in values:
        normalized = _material_json_value(value)
        if normalized is not None:
            return normalized
    return None


def _decode_json_object_list(raw: Any) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Decode a JSON/list value that must contain objects only."""
    val = _material_json_value(raw)
    if val is None:
        return [], None
    parsed: Any = val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
        except (TypeError, ValueError) as e:
            return [], f"decode_error:{type(e).__name__}"
    if not isinstance(parsed, list):
        return [], "decode_error:not_a_list"
    normalized: List[Dict[str, Any]] = []
    for item in parsed:
        safe_item = _type_json_safe(item)
        if not isinstance(safe_item, dict):
            return [], "decode_error:item_not_object"
        normalized.append(
            {
                str(key): _type_json_safe(value)
                for key, value in safe_item.items()
            }
        )
    return normalized, None


def _decode_type_use_observations(
    raw: Any, *, max_observations: int
) -> tuple[List[Dict[str, Any]], int, Optional[str]]:
    """Decode/normalize uses_type observations JSON into a bounded sample.

    Returns ``(sample, total_count, decode_error_or_none)``. Never invents
    observation facts on malformed input.
    """
    if max_observations < 0:
        max_observations = 0
    observations, error = _decode_json_object_list(raw)
    if error:
        return [], 0, error
    return observations[:max_observations], len(observations), None


def compact_relationship(
    rel: Dict[str, Any],
    *,
    max_type_observations: int = 5,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": rel.get("id"),
        "source": rel.get("source"),
        "target": rel.get("target"),
        "type": rel.get("type"),
        "description": rel.get("description"),
        "weight": rel.get("weight"),
    }
    # Surface preprocessor provenance on edges so a porting agent sees
    # configuration-conditional call sites without digging into parquet columns.
    if "preprocessor_dependent" in rel and _as_bool(rel.get("preprocessor_dependent")):
        out["preprocessor_dependent"] = True
        reasons = _reasons_list(rel.get("preprocessor_reasons"))
        if reasons:
            out["preprocessor_reasons"] = reasons
    # Python dynamic-dispatch provenance (same shape).
    if "dynamic_dependent" in rel and _as_bool(rel.get("dynamic_dependent")):
        out["dynamic_dependent"] = True
        reasons = _reasons_list(rel.get("dynamic_reasons"))
        if reasons:
            out["dynamic_reasons"] = reasons

    # Configured uses_type evidence (bounded; never unbounded raw JSON strings).
    if str(rel.get("type", "")) == "uses_type":
        fact_kind = _first_material_json_value(
            rel.get("fact_kind"), rel.get("clang_type_use_fact_kind")
        )
        if fact_kind is not None:
            out["fact_kind"] = fact_kind
        extractor = _first_material_json_value(
            rel.get("extractor"), rel.get("clang_type_use_extractor")
        )
        if extractor is not None:
            out["extractor"] = extractor
        conf = _first_material_json_value(
            rel.get("clang_type_use_confidence"), rel.get("confidence")
        )
        if conf is not None:
            out["confidence"] = conf
        det = _first_material_json_value(
            rel.get("clang_type_use_is_deterministic"),
            rel.get("is_deterministic"),
        )
        if det is not None:
            out["is_deterministic"] = _as_bool(det)

        obs_count = _material_json_value(
            rel.get("clang_type_use_observation_count")
        )
        normalized_obs_count: Optional[int] = None
        if (
            isinstance(obs_count, (int, float))
            and not isinstance(obs_count, bool)
            and math.isfinite(float(obs_count))
            and float(obs_count).is_integer()
            and obs_count >= 0
        ):
            normalized_obs_count = int(obs_count)
            out["observation_count"] = normalized_obs_count

        use_kinds = _to_list(rel.get("clang_type_use_use_kinds"))
        cleaned_use_kinds = sorted(
            {
                str(value)
                for value in (
                    _material_json_value(item) for item in use_kinds
                )
                if value is not None
            }
        )
        if cleaned_use_kinds:
            out["use_kinds"] = cleaned_use_kinds

        entry_indices = _to_list(rel.get("clang_type_use_entry_indices"))
        if entry_indices:
            cleaned: List[int] = []
            for x in entry_indices:
                sx = _material_json_value(x)
                if isinstance(sx, bool):
                    continue
                if (
                    isinstance(sx, (int, float))
                    and math.isfinite(float(sx))
                    and float(sx).is_integer()
                    and sx >= 0
                ):
                    cleaned.append(int(sx))
            if cleaned:
                out["entry_indices"] = sorted(set(cleaned))

        cpath = _material_json_value(rel.get("clang_type_use_compiler_path"))
        cid = _material_json_value(rel.get("clang_type_use_compiler_id"))
        if cpath is not None:
            out["compiler_path"] = cpath
        if cid is not None:
            out["compiler_id"] = cid
        compilers_raw = rel.get("clang_type_use_compilers")
        if _material_json_value(compilers_raw) is not None:
            compilers, compiler_error = _decode_json_object_list(compilers_raw)
            if compiler_error:
                out["compilers_decode_error"] = compiler_error
            else:
                out["compilers"] = compilers

        digest = _material_json_value(
            rel.get("clang_type_use_compile_commands_digest")
        )
        if digest is not None:
            out["compile_commands_digest"] = digest

        src_eid = _material_json_value(
            rel.get("clang_type_use_source_entity_id")
        )
        tgt_eid = _material_json_value(
            rel.get("clang_type_use_target_entity_id")
        )
        if src_eid is not None:
            out["source_entity_id"] = src_eid
        if tgt_eid is not None:
            out["target_entity_id"] = tgt_eid

        sample, total, err = _decode_type_use_observations(
            rel.get("clang_type_use_observations_json"),
            max_observations=max_type_observations,
        )
        out["observation_sample"] = sample
        out["observation_sample_count"] = len(sample)
        out["observation_total_count"] = total
        out["observation_truncated"] = total > len(sample)
        if normalized_obs_count is not None and normalized_obs_count != total:
            out["observation_count_mismatch"] = {
                "declared": normalized_obs_count,
                "decoded": total,
            }
        if err:
            out["observation_decode_error"] = err
            out["observation_sample"] = []
            out["observation_sample_count"] = 0
    return out


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
    graph: Path = typer.Option(Path("byog_mini_game"), "--graph", "-g", help="BYOG graph root (snapshot layout or legacy output/ layout)"),
    purpose: str = typer.Option("port-to-rust", "--purpose", "-p"),
    output: Path | None = typer.Option(None, "--output", "-o", help="Write JSON to this path instead of stdout"),
    max_text_chars: int = typer.Option(300, "--max-text-chars", help="Truncate text units to this many chars (0 or negative = no limit)"),
    full_text: bool = typer.Option(False, "--full-text", help="Equivalent to --max-text-chars 0 (no truncation)"),
    neighbor_text: bool = typer.Option(
        True,
        "--neighbor-text/--no-neighbor-text",
        help="Include text units attached to neighbor relationships",
    ),
    max_type_edges: int = typer.Option(
        20,
        "--max-type-edges",
        help=(
            "Max uses_type edges per direction; at depth > 1 this also caps "
            "returned closure-node payloads"
        ),
    ),
    max_type_observations: int = typer.Option(
        5,
        "--max-type-observations",
        help="Max observations sampled per uses_type edge in the pack",
    ),
    type_depth: int = typer.Option(
        1,
        "--type-depth",
        help=(
            "uses_type depth for context packs (default 1 = direct only; "
            "depth > 1 adds transitive type_*_closure sections)"
        ),
    ),
):
    """Assemble and print (or save) a context pack for the given symbol."""
    if full_text:
        max_text_chars = 0
    if max_type_edges < 0:
        max_type_edges = 0
    if max_type_observations < 0:
        max_type_observations = 0
    if isinstance(type_depth, bool) or not isinstance(type_depth, int) or type_depth < 1:
        typer.secho(
            f"--type-depth must be a positive integer, got {type_depth!r}",
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    try:
        data = load_byog(graph)
    except FileNotFoundError:
        typer.secho(f"BYOG not found under {graph}. Run an indexer or bridge first.", fg=typer.colors.RED)
        raise typer.Exit(1)
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
    texts = get_text_units(tus, ent, neighbors if neighbor_text else [])

    entity_fields = {
        k: _json_safe(v) for k, v in ent_dict.items()
        if k in (
            "id", "title", "type", "description", "source_file", "span",
            "extractor", "confidence", "is_deterministic",
            # C frontend honesty: tree-sitter cannot resolve the preprocessor.
            "preprocessor_dependent", "preprocessor_reasons", "preprocessor_branches",
            "preprocessor_eval_mode", "preprocessor_macro_seed_digest",
            # Python frontend honesty: syntax/AST cannot follow dynamic dispatch.
            "dynamic_dependent", "dynamic_reasons",
        )
    }
    # Normalize reasons to plain str lists (parquet may yield ndarrays).
    if "preprocessor_reasons" in entity_fields:
        entity_fields["preprocessor_reasons"] = _reasons_list(entity_fields.get("preprocessor_reasons"))
    if "preprocessor_dependent" in entity_fields:
        entity_fields["preprocessor_dependent"] = _as_bool(entity_fields.get("preprocessor_dependent"))
    if "preprocessor_branches" in entity_fields:
        entity_fields["preprocessor_branches"] = _json_safe(entity_fields.get("preprocessor_branches"))
    if "dynamic_reasons" in entity_fields:
        entity_fields["dynamic_reasons"] = _reasons_list(entity_fields.get("dynamic_reasons"))
    if "dynamic_dependent" in entity_fields:
        entity_fields["dynamic_dependent"] = _as_bool(entity_fields.get("dynamic_dependent"))

    pack: Dict[str, Any] = {
        "symbol": ent_dict.get("title"),
        "purpose": purpose,
        "entity": entity_fields,
        "neighbors": [
            compact_relationship(nr, max_type_observations=max_type_observations)
            for nr in neighbors[:30]
        ],
    }

    # Make preprocessor dependence impossible to miss: top-level warning +
    # structured summary. Detection only — not macro expansion or types.
    entity_pp = _as_bool(ent_dict.get("preprocessor_dependent"))
    entity_reasons = _reasons_list(ent_dict.get("preprocessor_reasons"))
    flagged_neighbor_calls = [
        n for n in pack["neighbors"]
        if str(n.get("type", "")) == "calls" and n.get("preprocessor_dependent")
    ]
    # Structured branch liveness under compile_commands -D + header defaults
    # (live / dead / unknown). Never invents a decision for platform macros.
    def _normalize_branches(raw: Any) -> List[Dict[str, Any]]:
        items = _json_safe(raw)
        if items is None:
            return []
        if not isinstance(items, list):
            items = [items]
        out: List[Dict[str, Any]] = []
        for b in items:
            b = _json_safe(b)
            if isinstance(b, dict):
                out.append({
                    "kind": str(b.get("kind", "")),
                    "condition": str(b.get("condition", "")),
                    "start_line": int(b.get("start_line") or 0),
                    "end_line": int(b.get("end_line") or 0),
                    "liveness": str(b.get("liveness", "unknown")),
                    "basis": str(b.get("basis", "")),
                })
            elif isinstance(b, str) and b:
                # tolerate JSON-encoded branch rows from awkward parquet types
                try:
                    parsed = json.loads(b)
                    if isinstance(parsed, dict):
                        out.append(parsed)
                except Exception:
                    pass
        return out

    entity_branches: List[Dict[str, Any]] = []
    if "preprocessor_branches" in ent_dict or "preprocessor_dependent" in ent_dict:
        entity_branches = _normalize_branches(ent_dict.get("preprocessor_branches"))
        if "preprocessor_branches" in entity_fields:
            entity_fields["preprocessor_branches"] = entity_branches

    if entity_pp or flagged_neighbor_calls:
        sample_reasons = entity_reasons[:10]
        if not sample_reasons and flagged_neighbor_calls:
            sample_reasons = _reasons_list(flagged_neighbor_calls[0].get("preprocessor_reasons"))[:10]
        live_n = sum(1 for b in entity_branches if b.get("liveness") == "live")
        dead_n = sum(1 for b in entity_branches if b.get("liveness") == "dead")
        unk_n = sum(1 for b in entity_branches if b.get("liveness") == "unknown")
        live_bits = [
            f"{b.get('kind')}({str(b.get('condition'))[:30]})"
            for b in entity_branches if b.get("liveness") == "live"
        ][:6]
        dead_bits = [
            f"{b.get('kind')}({str(b.get('condition'))[:30]})"
            for b in entity_branches if b.get("liveness") == "dead"
        ][:6]
        _eval_mode = _json_safe(ent_dict.get("preprocessor_eval_mode"))
        pack["preprocessor_warning"] = (
            "PREPROCESSOR-DEPENDENT (tree-sitter C frontend): this symbol and/or "
            "its call edges sit inside #if/#ifdef regions or involve function-like "
            f"macros. Branch liveness (eval_mode={_eval_mode or 'no_compiler'}) "
            f"is reported as weak provenance (live={live_n}, dead={dead_n}, "
            f"unknown={unk_n}"
            + (f"; live_regions={live_bits}" if live_bits else "")
            + (f"; dead_regions={dead_bits}" if dead_bits else "")
            + "). unknown must not be treated as dead. Labels do not demote "
            f"is_deterministic. entity_flagged={entity_pp}; "
            f"flagged_neighbor_calls={len(flagged_neighbor_calls)}; "
            f"sample_reasons={sample_reasons}."
        )
        pack["preprocessor"] = {
            "entity_dependent": entity_pp,
            "entity_reasons": entity_reasons,
            "flagged_neighbor_calls": len(flagged_neighbor_calls),
            "flagged_call_targets": sorted({
                str(n.get("target")) for n in flagged_neighbor_calls if n.get("target")
            }),
            # Parallel to dynamic.dispatch_candidates: names the work (which
            # branches are live/dead under the recorded build), still weak.
            "branch_liveness": entity_branches,
            "eval_mode": _json_safe(ent_dict.get("preprocessor_eval_mode"))
            or (
                (entity_branches[0].get("eval_mode") if entity_branches else None)
            ),
            "macro_seed_digest": _json_safe(
                ent_dict.get("preprocessor_macro_seed_digest")
            ),
            "note": (
                "Detection + weak liveness under compile_commands -D and header "
                "defaults (scripts/c_preprocessor.py). Published default is "
                "eval_mode=no_compiler (host-independent; platform macros unknown). "
                "Local --compiler-builtins seeds toolchain tables with "
                "basis=builtin:NAME=… and records macro_seed_digest in the "
                "snapshot manifest. Not full clang expansion of arbitrary expressions."
            ),
        }

    # Python analogue: dynamic-dispatch dependence (registry tables, getattr with
    # non-literal names, polymorphic receivers). Same shape as preprocessor_*.
    entity_dyn = _as_bool(ent_dict.get("dynamic_dependent"))
    entity_dyn_reasons = _reasons_list(ent_dict.get("dynamic_reasons"))
    flagged_dyn_neighbor_calls = [
        n for n in pack["neighbors"]
        if str(n.get("type", "")) == "calls" and n.get("dynamic_dependent")
    ]
    if entity_dyn or flagged_dyn_neighbor_calls:
        sample_dyn = entity_dyn_reasons[:10]
        if not sample_dyn and flagged_dyn_neighbor_calls:
            sample_dyn = _reasons_list(flagged_dyn_neighbor_calls[0].get("dynamic_reasons"))[:10]
        pack["dynamic_warning"] = (
            "DYNAMIC-DISPATCH-DEPENDENT (Python syntax/AST frontend): this symbol "
            "and/or its call edges use registry/dict callable tables, getattr with "
            "a non-literal name, or polymorphic receivers the extractor cannot "
            "follow. Missing callees are often runtime-chosen, not absent. Labels "
            "are provenance only and do not demote is_deterministic. "
            f"entity_flagged={entity_dyn}; "
            f"flagged_neighbor_calls={len(flagged_dyn_neighbor_calls)}; "
            f"sample_reasons={sample_dyn}."
        )
        pack["dynamic"] = {
            "entity_dependent": entity_dyn,
            "entity_reasons": entity_dyn_reasons,
            "flagged_neighbor_calls": len(flagged_dyn_neighbor_calls),
            "flagged_call_targets": sorted({
                str(n.get("target")) for n in flagged_dyn_neighbor_calls if n.get("target")
            }),
            # Filled after uncertain_calls are assembled (registry_candidate obs).
            "dispatch_candidates": [],
            "note": (
                "Detection only (scripts/python_dynamic.py). Registry member names "
                "may appear as weak dispatch_candidates / uncertain_calls with "
                "reason registry_candidate:* — static table members; also promoted "
                "to non-deterministic calls edges (is_deterministic=False) when "
                "the dispatch site is labelled."
            ),
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
        pack["module_neighbors"] = [
            compact_relationship(
                relationship,
                max_type_observations=max_type_observations,
            )
            for relationship in module_relationships[:100]
        ]

    # Golden contract note for the canonical mini_game graph only.  Symbol names
    # such as `sim` or `physics` are not sufficient evidence: they are common in
    # unrelated repositories and would contaminate a generic context pack.
    is_mini_game_graph = "mini_game" in Path(graph).name
    golden_note = ""
    if is_mini_game_graph:
        golden_note = (
            "GOLDEN BEHAVIOR CONTRACT: The mini_game simulator has committed golden traces (see examples/mini_game/tests/golden_*.json). "
            "All ports must pass the exact same state/collided/score traces for the defined input sequences, including the collision_first scenario (jumps=[6] produces collided=True at tick 9). "
            "See test_collision_first and test_golden_trace_matches."
        )

    def truncate_text(txt: str, limit: int) -> tuple[str, bool]:
        if limit <= 0 or len(txt) <= limit:
            return txt, False
        return txt[:limit], True

    # First-class data dependencies (module-level tables/constants read by the
    # symbol). These are intentionally separate from the generic text_units slice
    # so large table-driven ports do not accidentally lose their SQL_REGEX /
    # KEYWORDS_* equivalents just because they fall after the first N snippets.
    data_dep_rels = [r for r in neighbors if str(r.get("type", "")) == "uses_data"]
    if data_dep_rels:
        symbol_title = str(ent_dict.get("title", ""))
        symbol_id = str(ent_dict.get("id", ""))
        dep_refs: list[str] = []
        for r in data_dep_rels:
            for endpoint in (str(r.get("source", "")), str(r.get("target", ""))):
                if endpoint and endpoint not in {symbol_title, symbol_id} and endpoint not in dep_refs:
                    dep_refs.append(endpoint)
        if dep_refs:
            data_mask = (
                ents["title"].astype(str).isin(dep_refs)
                | ents["id"].astype(str).isin(dep_refs)
            )
            data_rows = ents[data_mask]
            deps = []
            for _, dep in data_rows.iterrows():
                tu_refs = [str(x) for x in _to_list(dep.get("text_unit_ids"))]
                raw_text = ""
                if tu_refs and len(tus) > 0:
                    tmask = tus["id"].astype(str).isin(tu_refs)
                    if tmask.any():
                        raw_text = str(tus[tmask].iloc[0].get("text", ""))
                dep_text, dep_truncated = truncate_text(raw_text, max_text_chars)
                deps.append({
                    "title": dep.get("title"),
                    "type": dep.get("type"),
                    "source_file": dep.get("source_file"),
                    "span": dep.get("span"),
                    "description": dep.get("description"),
                    "text": dep_text,
                    "truncated": dep_truncated,
                })
            if deps:
                pack["data_dependencies"] = deps
                pack["data_dependency_edges"] = [
                    compact_relationship(
                        r, max_type_observations=max_type_observations
                    )
                    for r in data_dep_rels
                ]

    # Configured uses_type evidence (optional Clang overlay). Collected from
    # the full neighbor relationship set — not the capped pack["neighbors"] —
    # so type edges remain visible even when they fall after the first 30
    # unrelated neighbors.
    symbol_title = str(ent_dict.get("title", ""))
    symbol_id = str(ent_dict.get("id", ""))
    uses_type_all = [
        r for r in neighbors if str(r.get("type", "")) == "uses_type"
    ]
    if uses_type_all:
        def _rel_sort_key(r: Dict[str, Any]) -> tuple:
            return (
                str(r.get("source", "")),
                str(r.get("target", "")),
                str(r.get("id", "")),
            )

        outgoing = sorted(
            [
                r
                for r in uses_type_all
                if str(r.get("source", "")) in {symbol_title, symbol_id}
            ],
            key=_rel_sort_key,
        )
        incoming = sorted(
            [
                r
                for r in uses_type_all
                if str(r.get("target", "")) in {symbol_title, symbol_id}
            ],
            key=_rel_sort_key,
        )
        # Self-edges (source == target) legitimately appear in both roles.
        if outgoing:
            out_slice = outgoing[:max_type_edges]
            pack["type_dependency_edges"] = [
                compact_relationship(
                    r, max_type_observations=max_type_observations
                )
                for r in out_slice
            ]
            target_refs: List[str] = []
            for r in out_slice:
                tgt = str(r.get("target", ""))
                if tgt and tgt not in target_refs:
                    target_refs.append(tgt)
            if target_refs and len(ents) > 0:
                data_mask = ents["title"].astype(str).isin(target_refs)
                type_rows = ents[data_mask]
                if len(type_rows) and "title" in type_rows.columns:
                    type_rows = type_rows.sort_values(by="title")
                type_deps: List[Dict[str, Any]] = []
                for _, dep in type_rows.iterrows():
                    tu_refs = [str(x) for x in _to_list(dep.get("text_unit_ids"))]
                    raw_text = ""
                    if tu_refs and len(tus) > 0:
                        tmask = tus["id"].astype(str).isin(tu_refs)
                        if tmask.any():
                            raw_text = str(tus[tmask].iloc[0].get("text", ""))
                    dep_text, dep_truncated = truncate_text(raw_text, max_text_chars)
                    type_deps.append(
                        {
                            "title": _json_safe(dep.get("title")),
                            "type": _json_safe(dep.get("type")),
                            "source_file": _json_safe(dep.get("source_file")),
                            "span": _json_safe(dep.get("span")),
                            "description": _json_safe(dep.get("description")),
                            "text": dep_text,
                            "truncated": dep_truncated,
                        }
                    )
                if type_deps:
                    pack["type_dependencies"] = type_deps
            pack["type_dependency_truncated"] = len(outgoing) > len(out_slice)
            pack["type_dependency_total"] = len(outgoing)

        if incoming:
            in_slice = incoming[:max_type_edges]
            pack["type_user_edges"] = [
                compact_relationship(
                    r, max_type_observations=max_type_observations
                )
                for r in in_slice
            ]
            pack["type_user_truncated"] = len(incoming) > len(in_slice)
            pack["type_user_total"] = len(incoming)

    # Transitive uses_type closure sections (consumer-only). Default
    # type_depth=1 keeps pack JSON byte-identical to the direct-only shape.
    if type_depth > 1:
        from byog_graph import compute_uses_type_closure  # type: ignore

        uses_by_id: Dict[str, Dict[str, Any]] = {}
        if len(rels) > 0 and "type" in rels.columns:
            for _, row in rels[rels["type"].astype(str) == "uses_type"].iterrows():
                raw_id = row.get("id")
                # Validation happens in compute_uses_type_closure(). Do not
                # coerce malformed IDs here: pd.NA, for example, cannot be
                # truth-tested and must reach the controlled fail-closed path.
                if isinstance(raw_id, str) and raw_id and raw_id not in uses_by_id:
                    uses_by_id[raw_id] = row.to_dict()

        def _closure_section(direction: str) -> Optional[Dict[str, Any]]:
            closure = compute_uses_type_closure(
                rels,
                symbol_title,
                direction=direction,
                max_depth=type_depth,
                max_nodes=max_type_edges,
                max_edges=max_type_edges,
            )
            if (
                int(closure.get("n_edges_total") or 0) == 0
                and int(closure.get("n_nodes_total") or 0) <= 1
            ):
                return None
            # Preserve a one-to-one correspondence with closure["nodes"]. A
            # dangling endpoint or duplicate entity title is corrupt graph
            # state, but silently dropping/duplicating it here would make the
            # returned counts and truncation flags dishonest.
            type_nodes: List[Dict[str, Any]] = []
            for closure_node in closure.get("nodes") or []:
                title = str(closure_node.get("title"))
                depth = int(closure_node.get("depth") or 0)
                if len(ents) > 0 and "title" in ents.columns:
                    matches = ents[ents["title"].astype(str) == title]
                else:
                    matches = ents.iloc[0:0]
                if len(matches) != 1:
                    type_nodes.append(
                        {
                            "title": title,
                            "depth": depth,
                            "entity_status": "missing" if len(matches) == 0 else "ambiguous",
                            "entity_match_count": int(len(matches)),
                            "text": "",
                            "truncated": False,
                        }
                    )
                    continue
                dep = matches.iloc[0]
                tu_refs = [str(x) for x in _to_list(dep.get("text_unit_ids"))]
                raw_text = ""
                if tu_refs and len(tus) > 0:
                    tmask = tus["id"].astype(str).isin(tu_refs)
                    if tmask.any():
                        raw_text = str(tus[tmask].iloc[0].get("text", ""))
                dep_text, dep_truncated = truncate_text(raw_text, max_text_chars)
                type_nodes.append(
                    {
                        "title": _json_safe(dep.get("title")),
                        "type": _json_safe(dep.get("type")),
                        "depth": depth,
                        "source_file": _json_safe(dep.get("source_file")),
                        "span": _json_safe(dep.get("span")),
                        "description": _json_safe(dep.get("description")),
                        "text": dep_text,
                        "truncated": dep_truncated,
                    }
                )
            compact_edges: List[Dict[str, Any]] = []
            for edge in closure.get("edges") or []:
                rid = str(edge.get("id") or "")
                raw_rel = uses_by_id.get(rid)
                if raw_rel is None:
                    # This cannot occur when closure and payloads use the same
                    # dataframe, but keep the corruption explicit if that
                    # invariant changes in a future refactor.
                    compact = {
                        "id": rid,
                        "source": edge.get("source"),
                        "target": edge.get("target"),
                        "type": "uses_type",
                        "relationship_status": "missing",
                    }
                else:
                    compact = compact_relationship(
                        raw_rel, max_type_observations=max_type_observations
                    )
                compact["depth"] = int(edge.get("depth") or 0)
                compact_edges.append(compact)
            return {
                "root": closure.get("root"),
                "direction": direction,
                "max_depth": type_depth,
                "nodes": type_nodes,
                "edges": compact_edges,
                "n_nodes_total": int(closure.get("n_nodes_total") or 0),
                "n_edges_total": int(closure.get("n_edges_total") or 0),
                "n_nodes_returned": len(type_nodes),
                "n_edges_returned": len(compact_edges),
                "nodes_truncated": bool(closure.get("nodes_truncated")),
                "edges_truncated": bool(closure.get("edges_truncated")),
            }

        try:
            dep_section = _closure_section("dependencies")
            if dep_section is not None:
                pack["type_dependency_closure"] = dep_section
            user_section = _closure_section("users")
            if user_section is not None:
                pack["type_user_closure"] = user_section
        except ValueError as e:
            typer.secho(str(e), fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from e

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
    provenance: Dict[str, Any] = {
        "source_file": _json_safe(ent_dict.get("source_file")),
        "span": _json_safe(ent_dict.get("span")),
        "extractor": _json_safe(ent_dict.get("extractor")),
        "confidence": _json_safe(ent_dict.get("confidence")),
        "is_deterministic": _json_safe(ent_dict.get("is_deterministic")),
    }
    if "preprocessor_dependent" in ent_dict:
        provenance["preprocessor_dependent"] = entity_pp
        if entity_reasons:
            provenance["preprocessor_reasons"] = entity_reasons
        if entity_branches:
            provenance["preprocessor_branches"] = entity_branches
    if "dynamic_dependent" in ent_dict:
        provenance["dynamic_dependent"] = entity_dyn
        if entity_dyn_reasons:
            provenance["dynamic_reasons"] = entity_dyn_reasons
    pack.update({
        "text_units": packed_texts,
        "provenance": provenance,
        "golden_contract_note": golden_note if golden_note else None,
        "behavior_contract": "examples/mini_game/tests/behavior_contract.json (load for machine-readable invariants and expected values per scenario)" if is_mini_game_graph else None,
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
            if is_module_pack:
                mask = (obs_src == symbol_title) | obs_src.str.startswith(module_prefix)
            else:
                mask = (obs_src == symbol_title) | obs_src.str.startswith(symbol_title + ".")
            matched = obs[mask]
            # Prefer registry_candidate rows so dispatch targets are not truncated
            # out of the uncertain_calls window. Both halves stay capped: a large
            # registry must not crowd the rest of the pack out either.
            if len(matched) > 0 and "reason" in matched.columns:
                reasons_s = matched["reason"].astype(str)
                reg_rows = matched[reasons_s.str.startswith("registry_candidate:")].head(15)
                other_rows = matched[~reasons_s.str.startswith("registry_candidate:")]
                relevant = pd.concat([reg_rows, other_rows.head(max(0, 15 - len(reg_rows)))], axis=0)
            else:
                relevant = matched.head(15)
            if len(relevant) > 0:
                uncertain = []
                for _, o in relevant.iterrows():
                    entry: Dict[str, Any] = {
                        "source": str(o.get("source", "")),
                        "display_target": str(o.get("display_target", "")),
                        "confidence": float(o.get("confidence", 0.0) or 0.0),
                        "reason": str(o.get("reason", "")),
                        "provenance": f"{o.get('source_file', '')}:{o.get('span', '')}",
                    }
                    if "preprocessor_dependent" in o.index and _as_bool(o.get("preprocessor_dependent")):
                        entry["preprocessor_dependent"] = True
                        reasons = _reasons_list(o.get("preprocessor_reasons"))
                        if reasons:
                            entry["preprocessor_reasons"] = reasons
                    if "dynamic_dependent" in o.index and _as_bool(o.get("dynamic_dependent")):
                        entry["dynamic_dependent"] = True
                        reasons = _reasons_list(o.get("dynamic_reasons"))
                        if reasons:
                            entry["dynamic_reasons"] = reasons
                    uncertain.append(entry)
                pack["uncertain_calls"] = uncertain
                pack["analysis_note"] = "Some call sites were tracked with low confidence or ambiguity (see uncertain_calls). Review during port."
                # Promote static registry candidates into the dynamic summary so a
                # porting agent sees *which* implementations dispatch may reach,
                # not only that dispatch exists.
                if pack.get("dynamic") is not None:
                    cands = []
                    seen_disp: set[str] = set()
                    for u in uncertain:
                        reason = str(u.get("reason", ""))
                        if not reason.startswith("registry_candidate:"):
                            continue
                        disp = str(u.get("display_target", ""))
                        if disp in seen_disp:
                            continue
                        seen_disp.add(disp)
                        cands.append({
                            "display_target": disp,
                            "confidence": u.get("confidence"),
                            "reason": reason,
                        })
                    pack["dynamic"]["dispatch_candidates"] = cands
                    if cands:
                        pack["dynamic_warning"] = (
                            str(pack.get("dynamic_warning") or "")
                            + f" dispatch_candidates={len(cands)} "
                            f"({', '.join(str(c.get('display_target')) for c in cands[:8])}"
                            f"{', …' if len(cands) > 8 else ''})."
                        ).strip()
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
