#!/usr/bin/env python
"""Optional Clang configured direct-call evidence overlay.

Attaches compiler-confirmed direct-call metadata to **existing** tree-sitter
``calls`` relationships using ``run_clang_call_audit`` matched_internal rows.

This is **not**:
  * a new entity/relationship layer
  * points-to / function-pointer target resolution
  * multi-config, C++, ABI, or macro-complete call proof
  * silent attachment for residual / unconfirmed audit rows

Only one-to-one exact matches (caller/target titles + package-relative path +
tree_sitter_span + byte_offset) are published. Validation is atomic: any error
leaves ``data`` unchanged.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from c_clang_call_audit import (  # type: ignore
    ClangCallAuditError,
    parse_tree_sitter_call_span,
    run_clang_call_audit,
    source_byte_offset,
)
from c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    build_disabled_overlay_provenance,
)
from c_identities import package_relative_posix  # type: ignore

MODE = "clang_configured_call_overlay"
FACT_KIND = "configured_direct_call"
EXTRACTOR = "clang-ast-json"
CONFIDENCE_BOUNDARY = (
    "clang_call_confidence=1.0 and clang_call_is_deterministic=true mean the "
    "direct-call confirmation is re-derivable from the recorded Clang + "
    "compile_commands.json configuration via the AST call-site audit, not that "
    "the base tree-sitter edge is upgraded, ABI-complete, multi-config, or free "
    "of residuals. tree_sitter_only_internal, out_of_compile_db_scope, "
    "external_direct, and indirect remain observation-only residuals without "
    "invented configured facts."
)

_CALL_FIELDS = (
    "clang_call_status",
    "clang_call_fact_kind",
    "clang_call_extractor",
    "clang_call_confidence",
    "clang_call_is_deterministic",
    "clang_call_match_basis",
    "clang_call_byte_offset",
    "clang_call_entry_indices",
    "clang_call_compile_commands_digest",
    "clang_call_compiler_path",
    "clang_call_compiler_id",
    "clang_call_compilers_json",
    "clang_call_resolve_reason",
    "clang_call_ref_kind",
    "clang_call_ref_type",
    "clang_call_observations_json",
    "clang_call_description",
)

_BUCKET_KEYS = (
    "matched_internal",
    "clang_only_internal",
    "tree_sitter_only_internal",
    "external_direct",
    "indirect",
    "ambiguous",
    "macro_location_unsupported",
    "out_of_compile_db_scope",
)

_BUCKET_CLASSIFICATIONS = {
    "clang_only_internal": "internal_direct",
    "tree_sitter_only_internal": "tree_sitter_only_internal",
    "external_direct": "external_direct",
    "indirect": "indirect",
    "ambiguous": "ambiguous",
    "macro_location_unsupported": "macro_location_unsupported",
    "out_of_compile_db_scope": "out_of_compile_db_scope",
}


class ClangCallOverlayError(CompilerOverlayError):
    """Raised when the configured call overlay cannot apply honestly."""


def build_disabled_provenance() -> Dict[str, Any]:
    """Stable manifest block when ``--clang-calls`` is off."""
    return build_disabled_overlay_provenance()


def _rel_source(raw: str, package_dir: Path, *, context: str) -> str:
    package_dir = package_dir.resolve()
    if not raw:
        raise ClangCallOverlayError(f"{context} has empty source_file")
    p = Path(raw)
    if not p.is_absolute():
        p = (package_dir / p).resolve()
    else:
        p = p.resolve()
    try:
        return package_relative_posix(p, package_dir)
    except (OSError, ValueError) as e:
        raise ClangCallOverlayError(
            f"cannot normalize source_file {raw!r} for {context}: {e}"
        ) from e


def _entry_indices(value: Any, *, context: str) -> List[int]:
    if not isinstance(value, list) or not value:
        raise ClangCallOverlayError(
            f"{context} must have a non-empty list entry_indices"
        )
    out: Set[int] = set()
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ClangCallOverlayError(
                f"{context} has invalid compile entry index {raw!r}"
            )
        out.add(raw)
    if len(out) != len(value):
        raise ClangCallOverlayError(f"{context} contains duplicate entry indices")
    return sorted(out)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _canonical_object_list_json(
    value: Any, *, context: str, require_nonempty: bool = False
) -> str:
    """Canonical JSON for a list of objects (deep key-sort + list sort)."""
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise ClangCallOverlayError(f"{context} must be a list of objects")
    if require_nonempty and not value:
        raise ClangCallOverlayError(f"{context} must be a non-empty list")
    try:
        normalized = [dict(x) for x in value]
        normalized.sort(key=_canonical_json)
        return _canonical_json(normalized)
    except (TypeError, ValueError) as e:
        raise ClangCallOverlayError(
            f"{context} has non-JSON-serializable content: {e}"
        ) from e


def _values_compatible(existing: Any, new: Any) -> bool:
    if existing is None or (
        isinstance(existing, float) and math.isnan(existing)
    ):
        return True
    if (
        isinstance(existing, (int, float))
        and not isinstance(existing, bool)
        and isinstance(new, (int, float))
        and not isinstance(new, bool)
    ):
        return float(existing) == float(new)
    if isinstance(existing, list) and isinstance(new, list):
        try:
            return [_canonical_json(x) for x in existing] == [
                _canonical_json(x) for x in new
            ]
        except TypeError:
            return list(existing) == list(new)
    return type(existing) is type(new) and existing == new


def _relationship_byte_offset(
    rel: Dict[str, Any],
    package_dir: Path,
    *,
    cache: Dict[Path, Tuple[List[bytes], List[int]]],
) -> Optional[int]:
    """Derive tree-sitter call-site byte offset from span + source_file."""
    line, col0 = parse_tree_sitter_call_span(str(rel.get("span") or ""))
    if line is None or col0 is None:
        return None
    raw = str(rel.get("source_file") or "")
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = (package_dir / p).resolve()
    else:
        p = p.resolve()
    return source_byte_offset(p, line, col0, cache=cache)


def _validated_report(
    report: Dict[str, Any],
    *,
    package_dir: Path,
    n_base_calls: int,
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int], Dict[str, Any]]:
    """Validate report shape/accounting before mutation planning."""
    if str(report.get("mode") or "") != "clang_ast_json_call_audit":
        raise ClangCallOverlayError(
            f"unexpected call audit mode {report.get('mode')!r}; expected "
            "clang_ast_json_call_audit"
        )
    if str(report.get("package") or "") != package_dir.name:
        raise ClangCallOverlayError(
            f"call audit package {report.get('package')!r} does not match "
            f"target package {package_dir.name!r}"
        )

    digest = report.get("compile_commands_digest")
    if not isinstance(digest, str) or not digest.strip():
        raise ClangCallOverlayError(
            "call audit report has empty compile_commands_digest"
        )
    digest = digest.strip()

    n_compile_entries = report.get("n_compile_entries")
    if (
        isinstance(n_compile_entries, bool)
        or not isinstance(n_compile_entries, int)
        or n_compile_entries <= 0
    ):
        raise ClangCallOverlayError(
            "call audit n_compile_entries must be a positive integer"
        )
    tus = report.get("translation_units")
    if not isinstance(tus, list) or not all(isinstance(t, dict) for t in tus):
        raise ClangCallOverlayError(
            "call audit translation_units must be a list of objects"
        )

    counts_raw = report.get("counts")
    if not isinstance(counts_raw, dict):
        raise ClangCallOverlayError("call audit report has no counts object")

    buckets: Dict[str, List[Dict[str, Any]]] = {}
    counts: Dict[str, int] = {}
    for key in _BUCKET_KEYS:
        rows = report.get(key)
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) for r in rows
        ):
            raise ClangCallOverlayError(
                f"call audit bucket {key!r} must be a list of objects"
            )
        raw_count = counts_raw.get(key)
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 0
        ):
            raise ClangCallOverlayError(
                f"call audit count {key!r} must be a non-negative integer"
            )
        if raw_count != len(rows):
            raise ClangCallOverlayError(
                f"call audit count/list mismatch for {key!r}: "
                f"count={raw_count} rows={len(rows)}"
            )
        buckets[key] = list(rows)
        counts[key] = raw_count

    for bucket, expected in _BUCKET_CLASSIFICATIONS.items():
        for i, row in enumerate(buckets[bucket]):
            if row.get("classification") != expected:
                raise ClangCallOverlayError(
                    f"{bucket}[{i}] has unexpected classification "
                    f"{row.get('classification')!r}; expected {expected!r}"
                )

    accounting = report.get("tree_sitter_accounting")
    if not isinstance(accounting, dict):
        raise ClangCallOverlayError(
            "call audit report missing tree_sitter_accounting object"
        )
    for key in (
        "total_calls",
        "matched_internal",
        "covered_by_noninternal_clang_observation",
        "tree_sitter_only_internal",
        "out_of_compile_db_scope",
    ):
        val = accounting.get(key)
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            raise ClangCallOverlayError(
                f"tree_sitter_accounting.{key} must be a non-negative integer"
            )
    total = int(accounting["total_calls"])
    if total != n_base_calls:
        raise ClangCallOverlayError(
            f"tree_sitter_accounting.total_calls={total} disagrees with "
            f"base calls relationships={n_base_calls}"
        )
    parts = (
        int(accounting["matched_internal"])
        + int(accounting["covered_by_noninternal_clang_observation"])
        + int(accounting["tree_sitter_only_internal"])
        + int(accounting["out_of_compile_db_scope"])
    )
    if parts != total:
        raise ClangCallOverlayError(
            "tree_sitter_accounting components do not sum to total_calls: "
            f"{parts} != {total}"
        )
    if int(accounting["matched_internal"]) != counts["matched_internal"]:
        raise ClangCallOverlayError(
            "tree_sitter_accounting.matched_internal disagrees with "
            "counts.matched_internal"
        )
    if (
        int(accounting["tree_sitter_only_internal"])
        != counts["tree_sitter_only_internal"]
    ):
        raise ClangCallOverlayError(
            "tree_sitter_accounting.tree_sitter_only_internal disagrees with "
            "counts.tree_sitter_only_internal"
        )
    if (
        int(accounting["out_of_compile_db_scope"])
        != counts["out_of_compile_db_scope"]
    ):
        raise ClangCallOverlayError(
            "tree_sitter_accounting.out_of_compile_db_scope disagrees with "
            "counts.out_of_compile_db_scope"
        )

    # Fail-closed residuals for publication.
    bad = []
    for key in (
        "clang_only_internal",
        "ambiguous",
        "macro_location_unsupported",
    ):
        if counts[key]:
            bad.append(f"{key}={counts[key]}")
    covered = int(accounting["covered_by_noninternal_clang_observation"])
    if covered:
        bad.append(f"covered_by_noninternal_clang_observation={covered}")
    if bad:
        raise ClangCallOverlayError(
            "clang call overlay refuses unclean call-audit residuals: "
            + ", ".join(bad)
            + "; resolve or leave --clang-calls off"
        )

    # Compiler identity consistency.
    compilers = report.get("compilers") or []
    if not isinstance(compilers, list) or not compilers:
        raise ClangCallOverlayError(
            "call audit report must list at least one compiler identity"
        )
    allowed: Set[Tuple[str, str]] = set()
    normalized_compilers: List[Dict[str, Any]] = []
    for c in compilers:
        if not isinstance(c, dict):
            raise ClangCallOverlayError("compiler identity entry must be an object")
        path = c.get("compiler_path")
        cid = c.get("compiler_id")
        if not isinstance(path, str) or not path.strip():
            raise ClangCallOverlayError("compiler_path missing in report.compilers")
        if not isinstance(cid, str) or not cid.strip():
            raise ClangCallOverlayError("compiler_id missing in report.compilers")
        version = c.get("compiler_version")
        if version is not None and (
            not isinstance(version, str) or not version.strip()
        ):
            raise ClangCallOverlayError(
                "invalid compiler_version in report.compilers"
            )
        identity = (path.strip(), cid.strip())
        if identity in allowed:
            raise ClangCallOverlayError(
                f"duplicate compiler identity in report.compilers: {identity!r}"
            )
        allowed.add(identity)
        normalized_compilers.append(
            {
                "compiler_path": identity[0],
                "compiler_id": identity[1],
                "compiler_version": version.strip()
                if isinstance(version, str)
                else None,
            }
        )
    normalized_compilers.sort(
        key=lambda c: (c["compiler_path"], c["compiler_id"])
    )

    top_path = report.get("compiler_path")
    top_id = report.get("compiler_id")
    top_version = report.get("compiler_version")
    if len(allowed) == 1:
        only_path, only_id = next(iter(allowed))
        only_version = normalized_compilers[0]["compiler_version"]
        if (top_path, top_id, top_version) != (
            only_path,
            only_id,
            only_version,
        ):
            raise ClangCallOverlayError(
                "singular compiler identity disagrees with report.compilers"
            )
    elif any(value is not None for value in (top_path, top_id, top_version)):
        raise ClangCallOverlayError(
            "multi-compiler report must not expose a singular compiler identity"
        )

    if len(tus) != n_compile_entries:
        raise ClangCallOverlayError(
            "translation_units length disagrees with n_compile_entries"
        )
    entry_compilers: Dict[int, Tuple[str, str]] = {}
    for i, tu in enumerate(tus):
        ctx = f"translation_units[{i}]"
        entry_index = tu.get("entry_index")
        if (
            isinstance(entry_index, bool)
            or not isinstance(entry_index, int)
            or entry_index < 0
            or entry_index >= n_compile_entries
        ):
            raise ClangCallOverlayError(f"{ctx} has invalid entry_index")
        if entry_index in entry_compilers:
            raise ClangCallOverlayError(
                f"duplicate translation unit entry_index {entry_index}"
            )
        tu_path = tu.get("compiler_path")
        tu_id = tu.get("compiler_id")
        if not isinstance(tu_path, str) or not tu_path.strip():
            raise ClangCallOverlayError(f"{ctx} has empty compiler_path")
        if not isinstance(tu_id, str) or not tu_id.strip():
            raise ClangCallOverlayError(f"{ctx} has empty compiler_id")
        identity = (tu_path.strip(), tu_id.strip())
        if identity not in allowed:
            raise ClangCallOverlayError(
                f"{ctx} names compiler absent from report.compilers"
            )
        entry_compilers[entry_index] = identity
    if set(entry_compilers) != set(range(n_compile_entries)):
        raise ClangCallOverlayError(
            "translation unit entry indices do not cover all compile entries"
        )
    if set(entry_compilers.values()) != allowed:
        raise ClangCallOverlayError(
            "report.compilers includes an identity unused by translation_units"
        )

    # Validate each matched row shape.
    for i, row in enumerate(buckets["matched_internal"]):
        ctx = f"matched_internal[{i}]"
        for field in (
            "caller_title",
            "target_title",
            "source_path",
            "tree_sitter_span",
            "match_basis",
            "compile_commands_digest",
        ):
            if not isinstance(row.get(field), str) or not str(row.get(field)).strip():
                raise ClangCallOverlayError(f"{ctx} has empty {field}")
        if str(row.get("compile_commands_digest")) != digest:
            raise ClangCallOverlayError(
                f"{ctx} compile_commands_digest disagrees with report digest"
            )
        if row.get("ref_kind") != "FunctionDecl":
            raise ClangCallOverlayError(
                f"{ctx} ref_kind is not FunctionDecl"
            )
        basis = str(row.get("match_basis"))
        if basis not in {"exact_byte_offset", "exact_line_col_fallback"}:
            raise ClangCallOverlayError(
                f"{ctx} has unsupported match_basis {basis!r}"
            )
        # Require byte-offset identity when present (primary policy).
        bo = row.get("byte_offset")
        if bo is None:
            raise ClangCallOverlayError(
                f"{ctx} lacks tree-sitter byte_offset; refuse unconfirmed attach"
            )
        if isinstance(bo, bool) or not isinstance(bo, int) or bo < 0:
            raise ClangCallOverlayError(
                f"{ctx} has invalid byte_offset {bo!r}"
            )
        ts_line, ts_col0 = parse_tree_sitter_call_span(
            str(row.get("tree_sitter_span"))
        )
        if ts_line is None or ts_col0 is None:
            raise ClangCallOverlayError(
                f"{ctx} has an invalid tree_sitter_span"
            )
        if row.get("line") != ts_line or row.get("col0") != ts_col0:
            raise ClangCallOverlayError(
                f"{ctx} line/col0 disagree with tree_sitter_span"
            )
        cbo = row.get("clang_byte_offset")
        if cbo is not None and (
            isinstance(cbo, bool) or not isinstance(cbo, int) or cbo < 0
        ):
            raise ClangCallOverlayError(
                f"{ctx} has invalid clang_byte_offset {cbo!r}"
            )
        if basis == "exact_byte_offset":
            if cbo is None or int(cbo) != int(bo):
                raise ClangCallOverlayError(
                    f"{ctx} exact_byte_offset basis but offsets disagree "
                    f"ts={bo} clang={cbo}"
                )
        else:
            if cbo is not None:
                raise ClangCallOverlayError(
                    f"{ctx} line/column fallback must not carry a Clang byte offset"
                )
            clang_line = row.get("clang_line")
            clang_col1 = row.get("clang_col1")
            if (
                isinstance(clang_line, bool)
                or not isinstance(clang_line, int)
                or isinstance(clang_col1, bool)
                or not isinstance(clang_col1, int)
                or clang_line != ts_line
                or clang_col1 < 1
                or clang_col1 - 1 != ts_col0
            ):
                raise ClangCallOverlayError(
                    f"{ctx} has an unconfirmed exact_line_col_fallback"
                )
        entry_indices = _entry_indices(
            row.get("clang_entry_indices"), context=f"{ctx}.clang_entry_indices"
        )
        if any(index >= n_compile_entries for index in entry_indices):
            raise ClangCallOverlayError(
                f"{ctx}.clang_entry_indices contains an out-of-range index"
            )
        obs = row.get("clang_observations")
        if (
            not isinstance(obs, list)
            or not obs
            or not all(isinstance(o, dict) for o in obs)
        ):
            raise ClangCallOverlayError(
                f"{ctx}.clang_observations must be a non-empty list of objects"
            )
        comps = row.get("clang_compilers")
        if (
            not isinstance(comps, list)
            or not comps
            or not all(isinstance(c, dict) for c in comps)
        ):
            raise ClangCallOverlayError(
                f"{ctx}.clang_compilers must be a non-empty list of objects"
            )

        row_compilers: Set[Tuple[str, str]] = set()
        for j, compiler in enumerate(comps):
            cctx = f"{ctx}.clang_compilers[{j}]"
            cpath = compiler.get("compiler_path")
            cid = compiler.get("compiler_id")
            cdigest = compiler.get("compile_commands_digest")
            if not isinstance(cpath, str) or not cpath.strip():
                raise ClangCallOverlayError(f"{cctx} has empty compiler_path")
            if not isinstance(cid, str) or not cid.strip():
                raise ClangCallOverlayError(f"{cctx} has empty compiler_id")
            identity = (cpath.strip(), cid.strip())
            if identity not in allowed:
                raise ClangCallOverlayError(
                    f"{cctx} names compiler absent from report.compilers"
                )
            if cdigest != digest:
                raise ClangCallOverlayError(
                    f"{cctx} compile_commands_digest disagrees with report digest"
                )
            if identity in row_compilers:
                raise ClangCallOverlayError(
                    f"{ctx}.clang_compilers contains a duplicate identity"
                )
            row_compilers.add(identity)

        observation_entries: List[int] = []
        observation_compilers: Set[Tuple[str, str]] = set()
        for j, observation in enumerate(obs):
            octx = f"{ctx}.clang_observations[{j}]"
            entry_index = observation.get("entry_index")
            if (
                isinstance(entry_index, bool)
                or not isinstance(entry_index, int)
                or entry_index not in entry_indices
            ):
                raise ClangCallOverlayError(f"{octx} has invalid entry_index")
            if observation.get("classification") != "internal_direct":
                raise ClangCallOverlayError(
                    f"{octx} is not an internal_direct observation"
                )
            if observation.get("target_title") != row.get("target_title"):
                raise ClangCallOverlayError(
                    f"{octx} target_title disagrees with matched row"
                )
            if observation.get("ref_kind") != "FunctionDecl":
                raise ClangCallOverlayError(
                    f"{octx} ref_kind is not FunctionDecl"
                )
            if observation.get("ref_type") != row.get("ref_type"):
                raise ClangCallOverlayError(
                    f"{octx} ref_type disagrees with matched row"
                )
            opath = observation.get("compiler_path")
            oid = observation.get("compiler_id")
            odigest = observation.get("compile_commands_digest")
            if not isinstance(opath, str) or not opath.strip():
                raise ClangCallOverlayError(f"{octx} has empty compiler_path")
            if not isinstance(oid, str) or not oid.strip():
                raise ClangCallOverlayError(f"{octx} has empty compiler_id")
            identity = (opath.strip(), oid.strip())
            if identity != entry_compilers[entry_index]:
                raise ClangCallOverlayError(
                    f"{octx} compiler disagrees with its translation unit"
                )
            if odigest != digest:
                raise ClangCallOverlayError(
                    f"{octx} compile_commands_digest disagrees with report digest"
                )
            observation_entries.append(entry_index)
            observation_compilers.add(identity)
        if sorted(observation_entries) != entry_indices:
            raise ClangCallOverlayError(
                f"{ctx}.clang_observations do not cover entry_indices exactly"
            )
        if observation_compilers != row_compilers:
            raise ClangCallOverlayError(
                f"{ctx}.clang_compilers disagree with clang_observations"
            )

        # Singular path/id is present only for a single row-level compiler.
        path = row.get("compiler_path")
        cid = row.get("compiler_id")
        if len(row_compilers) == 1:
            if not isinstance(path, str) or not isinstance(cid, str):
                raise ClangCallOverlayError(
                    f"{ctx} lacks its singular compiler identity"
                )
            if (path, cid) != next(iter(row_compilers)):
                raise ClangCallOverlayError(
                    f"{ctx} singular compiler disagrees with observations"
                )
        elif path is not None or cid is not None:
            raise ClangCallOverlayError(
                f"{ctx} has singular compiler fields for multiple compilers"
            )

    return buckets, counts, {
        "digest": digest,
        "n_compile_entries": n_compile_entries,
        "translation_units": tus,
        "accounting": {
            k: int(accounting[k])
            for k in (
                "total_calls",
                "matched_internal",
                "covered_by_noninternal_clang_observation",
                "tree_sitter_only_internal",
                "out_of_compile_db_scope",
            )
        },
        "allowed_compilers": allowed,
        "compiler_path": report.get("compiler_path"),
        "compiler_id": report.get("compiler_id"),
        "compiler_version": report.get("compiler_version"),
        "compilers": normalized_compilers,
    }


def _payload_for_row(row: Dict[str, Any], *, digest: str) -> Dict[str, Any]:
    ctx = f"matched row {row.get('caller_title')}->{row.get('target_title')}"
    entry_indices = _entry_indices(
        row.get("clang_entry_indices"),
        context=ctx,
    )
    obs = list(row.get("clang_observations") or [])
    comps = list(row.get("clang_compilers") or [])
    if not comps and row.get("compiler_path"):
        comps = [
            {
                "compiler_path": row.get("compiler_path"),
                "compiler_id": row.get("compiler_id"),
                "compile_commands_digest": digest,
            }
        ]
    obs_json = _canonical_object_list_json(
        obs, context=f"{ctx}.clang_observations", require_nonempty=True
    )
    comps_json = _canonical_object_list_json(
        comps, context=f"{ctx}.clang_compilers", require_nonempty=True
    )
    desc = (
        f"configured Clang direct-call confirmation for "
        f"{row.get('caller_title')} -> {row.get('target_title')} "
        f"(fact_kind={FACT_KIND}; match_basis={row.get('match_basis')}; "
        f"confidence/determinism only relative to recorded Clang + "
        f"compile_commands.json)"
    )
    return {
        "clang_call_status": "matched",
        "clang_call_fact_kind": FACT_KIND,
        "clang_call_extractor": EXTRACTOR,
        "clang_call_confidence": 1.0,
        "clang_call_is_deterministic": True,
        "clang_call_match_basis": str(row.get("match_basis")),
        "clang_call_byte_offset": int(row["byte_offset"]),
        "clang_call_entry_indices": entry_indices,
        "clang_call_compile_commands_digest": digest,
        "clang_call_compiler_path": row.get("compiler_path"),
        "clang_call_compiler_id": row.get("compiler_id"),
        "clang_call_compilers_json": comps_json,
        "clang_call_resolve_reason": row.get("clang_resolve_reason"),
        "clang_call_ref_kind": row.get("ref_kind"),
        "clang_call_ref_type": row.get("ref_type"),
        "clang_call_observations_json": obs_json,
        "clang_call_description": desc,
    }


def _index_call_relationships(
    data: Dict[str, List[Dict[str, Any]]],
    package_dir: Path,
) -> Tuple[List[Dict[str, Any]], Dict[Tuple[str, str, str, str, int], List[Dict[str, Any]]]]:
    """Return all call rels and index by attachment key."""
    package_dir = package_dir.resolve()
    cache: Dict[Path, Tuple[List[bytes], List[int]]] = {}
    calls: List[Dict[str, Any]] = []
    index: Dict[Tuple[str, str, str, str, int], List[Dict[str, Any]]] = {}
    for rel in data.get("relationships") or []:
        if str(rel.get("type")) != "calls":
            continue
        calls.append(rel)
        src_title = str(rel.get("source") or "")
        tgt_title = str(rel.get("target") or "")
        span = str(rel.get("span") or "")
        rel_path = _rel_source(
            str(rel.get("source_file") or ""),
            package_dir,
            context=f"calls {src_title}->{tgt_title}",
        )
        bo = _relationship_byte_offset(rel, package_dir, cache=cache)
        if bo is None:
            # Keep unindexable; matching will fail if a matched row needs it.
            continue
        key = (src_title, tgt_title, rel_path, span, int(bo))
        index.setdefault(key, []).append(rel)
    return calls, index


def apply_clang_calls_from_report(
    data: Dict[str, List[Dict[str, Any]]],
    report: Dict[str, Any],
    package_dir: Path,
) -> Dict[str, Any]:
    """Apply a precomputed call-audit report onto ``data['relationships']``.

    Validates fully and builds a mutation plan before any write. On error,
    ``data`` is left unchanged.
    """
    package_dir = Path(package_dir).resolve()
    relationships = data.get("relationships")
    if not isinstance(relationships, list) or not all(
        isinstance(rel, dict) for rel in relationships
    ):
        raise ClangCallOverlayError("data.relationships must be a list of objects")
    # Snapshot for atomicity: we mutate only after full plan success; on error
    # raise before writes. Track planned writes then apply.
    base_calls = [
        r for r in relationships if str(r.get("type")) == "calls"
    ]
    buckets, counts, meta = _validated_report(
        report, package_dir=package_dir, n_base_calls=len(base_calls)
    )
    digest = meta["digest"]

    _calls, index = _index_call_relationships(data, package_dir)

    # Stale metadata: any relationship already carrying clang_call_* that is
    # not selected by this report must fail.
    planned: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    claimed_ids: Set[int] = set()

    matched_sorted = sorted(
        buckets["matched_internal"],
        key=lambda r: (
            str(r.get("source_path") or ""),
            str(r.get("caller_title") or ""),
            str(r.get("target_title") or ""),
            str(r.get("tree_sitter_span") or ""),
            int(r.get("byte_offset") if r.get("byte_offset") is not None else -1),
        ),
    )

    for row in matched_sorted:
        caller = str(row["caller_title"])
        target = str(row["target_title"])
        path = str(row["source_path"])
        span = str(row["tree_sitter_span"])
        bo = int(row["byte_offset"])
        key = (caller, target, path, span, bo)
        cands = index.get(key) or []
        if not cands:
            raise ClangCallOverlayError(
                f"no exact calls relationship for matched row "
                f"{caller}->{target} path={path} span={span} byte_offset={bo}"
            )
        if len(cands) > 1:
            raise ClangCallOverlayError(
                f"multiple calls relationships match matched row "
                f"{caller}->{target} path={path} span={span} byte_offset={bo}"
            )
        rel = cands[0]
        rel_id = id(rel)
        if rel_id in claimed_ids:
            raise ClangCallOverlayError(
                f"two matched rows claim the same relationship "
                f"{caller}->{target} span={span}"
            )
        claimed_ids.add(rel_id)
        payload = _payload_for_row(row, digest=digest)
        unknown = sorted(
            str(k)
            for k, value in rel.items()
            if str(k).startswith("clang_call_")
            and str(k) not in _CALL_FIELDS
            and value is not None
        )
        if unknown:
            raise ClangCallOverlayError(
                f"unknown pre-existing clang_call_* fields on "
                f"{caller}->{target}: {unknown}"
            )
        # Conflict preflight against existing fields.
        for k, new_val in payload.items():
            if k in rel and not _values_compatible(rel.get(k), new_val):
                raise ClangCallOverlayError(
                    f"conflicting pre-existing call field {k!r} on "
                    f"{caller}->{target}: existing={rel.get(k)!r} new={new_val!r}"
                )
        planned.append((rel, payload))

    # Stale metadata check: any relationship with clang_call_* not in planned.
    planned_rel_ids = {id(rel) for rel, _ in planned}
    for rel in relationships:
        if id(rel) in planned_rel_ids:
            continue
        stale = sorted(
            str(k)
            for k, value in rel.items()
            if str(k).startswith("clang_call_") and value is not None
        )
        if stale:
            raise ClangCallOverlayError(
                f"stale clang_call_* fields on unselected relationship "
                f"type={rel.get('type')} {rel.get('source')}->{rel.get('target')} "
                f"span={rel.get('span')}: "
                f"{stale}"
            )

    # Apply mutations only after full validation.
    n_changed = 0
    for rel, payload in planned:
        changed = False
        for k, new_val in payload.items():
            if rel.get(k) != new_val:
                rel[k] = new_val
                changed = True
        if changed:
            n_changed += 1

    return {
        "mode": MODE,
        "enabled": True,
        "fact_kind": FACT_KIND,
        "extractor": EXTRACTOR,
        "n_facts": len(planned),
        "n_facts_changed": n_changed,
        "n_compile_entries": meta["n_compile_entries"],
        "n_translation_units": len(meta["translation_units"]),
        "compiler_path": meta.get("compiler_path"),
        "compiler_id": meta.get("compiler_id"),
        "compiler_version": meta.get("compiler_version"),
        "compilers": meta["compilers"],
        "compile_commands_digest": digest,
        "counts": {
            k: counts[k]
            for k in _BUCKET_KEYS
        },
        "tree_sitter_accounting": meta["accounting"],
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }


def append_clang_calls(
    data: Dict[str, List[Dict[str, Any]]],
    package_dir: Path,
    *,
    timeout: int = 120,
    report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the call audit (unless ``report`` given) and attach call evidence."""
    package_dir = Path(package_dir).resolve()
    if timeout <= 0:
        raise ClangCallOverlayError(
            f"timeout must be a positive integer, got {timeout!r}"
        )
    if report is None:
        try:
            report = run_clang_call_audit(package_dir, timeout=timeout)
        except ClangCallAuditError as e:
            raise ClangCallOverlayError(str(e)) from e
    return apply_clang_calls_from_report(data, report, package_dir)
