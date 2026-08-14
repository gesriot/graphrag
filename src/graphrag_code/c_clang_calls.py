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

from graphrag_code.c_clang_call_audit import (  # type: ignore
    ClangCallAuditError,
    parse_tree_sitter_call_span,
    run_clang_call_audit,
    source_byte_offset,
)
from graphrag_code.c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    build_disabled_overlay_provenance,
)
from graphrag_code.c_identities import package_relative_posix  # type: ignore

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

# Audit-report contract text only; not a persisted manifest field.
LIMITATIONS = (
    "Configured direct-call confirmation of existing calls relationships only",
    "Not a new entity or relationship layer",
    "Not points-to or function-pointer target resolution",
    "Not ABI, multi-config, C++, or macro-complete call proof",
    "tree_sitter_only_internal, out_of_compile_db_scope, external_direct, "
    "and indirect invent no configured facts",
)

_ENABLED_MANIFEST_KEYS = (
    "mode",
    "enabled",
    "fact_kind",
    "extractor",
    "n_facts",
    "n_facts_changed",
    "n_compile_entries",
    "n_translation_units",
    "compiler_path",
    "compiler_id",
    "compiler_version",
    "compilers",
    "compile_commands_digest",
    "counts",
    "tree_sitter_accounting",
    "confidence_boundary",
)
_OFF_MANIFEST_KEYS = (
    "mode",
    "enabled",
    "n_facts",
    "n_translation_units",
)
_COUNT_KEYS = (
    "matched_internal",
    "clang_only_internal",
    "tree_sitter_only_internal",
    "external_direct",
    "indirect",
    "ambiguous",
    "macro_location_unsupported",
    "out_of_compile_db_scope",
)
_ACCOUNTING_KEYS = (
    "total_calls",
    "matched_internal",
    "covered_by_noninternal_clang_observation",
    "tree_sitter_only_internal",
    "out_of_compile_db_scope",
)
_FAIL_CLOSED_COUNT_KEYS = (
    "clang_only_internal",
    "ambiguous",
    "macro_location_unsupported",
)
_OBSERVATION_COUNT_KEYS = (
    "tree_sitter_only_internal",
    "out_of_compile_db_scope",
    "external_direct",
    "indirect",
)
_COMPILER_JSON_KEYS = (
    "compiler_path",
    "compiler_id",
    "compile_commands_digest",
)
_MANIFEST_COMPILER_KEYS = (
    "compiler_path",
    "compiler_id",
    "compiler_version",
)
_MATCH_BASES = frozenset({"exact_byte_offset", "exact_line_col_fallback"})

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
            if k not in rel or rel.get(k) != new_val:
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


# ---------------------------------------------------------------------------
# Persisted-overlay integrity contract (read-only; no Clang, no reindex)
# ---------------------------------------------------------------------------

_MAX_ANOMALY_SAMPLES = 40
_MAX_ANOMALY_MESSAGE = 400

ANOMALY_CODES = frozenset(
    {
        "empty_relationship_id",
        "duplicate_relationship_id",
        "legacy_block_missing_with_fields",
        "off_with_decorated_relationships",
        "invalid_enabled_block",
        "extra_manifest_key",
        "missing_manifest_key",
        "extra_count_key",
        "extra_accounting_key",
        "stale_call_metadata",
        "partial_call_payload",
        "unknown_call_field",
        "call_field_type",
        "identity_mismatch",
        "description_mismatch",
        "observations_json",
        "observation_record",
        "observation_coverage",
        "compilers_json",
        "compiler_mismatch",
        "digest_mismatch",
        "entry_index_census",
        "match_basis",
        "byte_offset",
        "manifest_mode_mismatch",
        "manifest_identity_mismatch",
        "manifest_count_mismatch",
        "manifest_contract_claim",
        "residual_bucket_nonzero",
        "accounting_mismatch",
    }
)


def is_material_value(value: Any) -> bool:
    """True when a parquet/JSON cell holds a real value (not null/NaN/NA)."""
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    try:
        import pandas as pd

        if value is pd.NA:
            return False
    except Exception:
        pass
    return True


def strict_json_loads(text: str) -> Any:
    """Decode standards-compliant JSON; reject NaN/Infinity and dup keys."""

    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON constant {token}")

    def unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate JSON object key {key!r}")
            out[key] = value
        return out

    return json.loads(
        text, parse_constant=reject_constant, object_pairs_hook=unique_object
    )


def _contains_non_finite(value: Any) -> bool:
    if isinstance(value, float):
        return not math.isfinite(value)
    if isinstance(value, dict):
        return any(
            _contains_non_finite(key) or _contains_non_finite(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_non_finite(item) for item in value)
    return False


def _strict_canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _scalar(value: Any) -> Any:
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _as_int(value: Any) -> Optional[int]:
    value = _scalar(value)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if not math.isfinite(value) or not float(value).is_integer():
            return None
        return int(value)
    return None


def _as_strict_int(value: Any) -> Optional[int]:
    """Return an integer only when the persisted value is genuinely integral."""
    value = _scalar(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return int(value)


def _as_bool(value: Any) -> Optional[bool]:
    value = _scalar(value)
    return value if isinstance(value, bool) else None


def _is_one(value: Any) -> bool:
    value = _scalar(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return float(value) == 1.0


def _clip(text: Any, limit: int = _MAX_ANOMALY_MESSAGE) -> str:
    s = str(text)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row.get(key, default)
    except Exception:
        try:
            return row[key]
        except Exception:
            return default


def _row_keys(row: Any) -> List[str]:
    try:
        return [str(k) for k in row.keys()]
    except Exception:
        return []


def _table_rows(table: Any, *, name: str) -> List[Dict[str, Any]]:
    if hasattr(table, "to_dict") and not isinstance(table, dict):
        try:
            records = table.to_dict("records")
        except (TypeError, ValueError, AttributeError) as error:
            raise TypeError(f"{name} dataframe cannot produce records") from error
    else:
        if isinstance(table, (str, bytes, dict)):
            raise TypeError(f"{name} must be a dataframe or sequence of rows")
        try:
            records = list(table)
        except TypeError as error:
            raise TypeError(
                f"{name} must be a dataframe or sequence of rows"
            ) from error
    out: List[Dict[str, Any]] = []
    for row in records:
        if isinstance(row, dict):
            out.append(row)
        elif hasattr(row, "items"):
            out.append(dict(row))
        else:
            raise TypeError(f"expected mapping row in {name}, got {type(row)!r}")
    return out


def _normalize_list_field(value: Any) -> Optional[List[Any]]:
    if not is_material_value(value):
        return None
    if isinstance(value, (list, tuple)):
        return list(value)
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        try:
            converted = value.tolist()
        except Exception:
            return None
        return converted if isinstance(converted, list) else None
    return None


def _anomaly(
    code: str,
    message: str,
    *,
    relationship_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if code not in ANOMALY_CODES:
        raise AssertionError(f"unknown call integrity anomaly code {code!r}")
    row: Dict[str, Any] = {"code": code, "message": _clip(message)}
    if relationship_id is not None:
        row["relationship_id"] = relationship_id
    if extra:
        for key, value in sorted(extra.items()):
            row[key] = _clip(value) if isinstance(value, str) else value
    return row


def _has_material_call_fields(row: Any) -> bool:
    for key in _row_keys(row):
        if key.startswith("clang_call_") and is_material_value(_row_get(row, key)):
            return True
    return False


def _producer_description(source: str, target: str, match_basis: str) -> str:
    return (
        f"configured Clang direct-call confirmation for {source} -> {target} "
        f"(fact_kind={FACT_KIND}; match_basis={match_basis}; "
        f"confidence/determinism only relative to recorded Clang + "
        f"compile_commands.json)"
    )


def _decoded_json_list(
    raw: Any,
    *,
    relationship_id: Optional[str],
    field: str,
    code: str,
    anomalies: List[Dict[str, Any]],
) -> Optional[List[Any]]:
    if not isinstance(raw, str) or not raw:
        anomalies.append(
            _anomaly(
                code,
                f"{field} is not a non-empty JSON string: {type(raw).__name__}",
                relationship_id=relationship_id,
            )
        )
        return None
    try:
        decoded = strict_json_loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        anomalies.append(
            _anomaly(
                code,
                f"{field} is not strict JSON: {error}",
                relationship_id=relationship_id,
            )
        )
        return None
    if _contains_non_finite(decoded):
        anomalies.append(
            _anomaly(
                code,
                f"{field} contains NaN/Infinity",
                relationship_id=relationship_id,
            )
        )
        return None
    try:
        canonical = _strict_canonical_json(decoded)
    except (TypeError, ValueError) as error:
        anomalies.append(
            _anomaly(
                code,
                f"{field} is not canonical-encodable: {error}",
                relationship_id=relationship_id,
            )
        )
        return None
    if canonical != raw:
        anomalies.append(
            _anomaly(
                code,
                f"{field} is not producer-canonical JSON",
                relationship_id=relationship_id,
            )
        )
        return None
    if not isinstance(decoded, list) or not decoded:
        anomalies.append(
            _anomaly(
                code,
                f"{field} must be a non-empty list",
                relationship_id=relationship_id,
            )
        )
        return None
    if not all(isinstance(item, dict) for item in decoded):
        anomalies.append(
            _anomaly(
                code,
                f"{field} must contain objects",
                relationship_id=relationship_id,
            )
        )
        return None
    expected_order = sorted(decoded, key=_strict_canonical_json)
    if decoded != expected_order:
        anomalies.append(
            _anomaly(
                code,
                f"{field} is not in producer canonical order",
                relationship_id=relationship_id,
            )
        )
        return None
    return decoded


def _decoded_indices(
    raw: Any,
    *,
    relationship_id: Optional[str],
    field: str,
    n_compile_entries: Optional[int],
    anomalies: List[Dict[str, Any]],
) -> Optional[List[int]]:
    values = _normalize_list_field(raw)
    if values is None or not values:
        anomalies.append(
            _anomaly(
                "entry_index_census",
                f"{field} is not a non-empty list",
                relationship_id=relationship_id,
            )
        )
        return None
    decoded = [_as_strict_int(index) for index in values]
    if any(index is None or index < 0 for index in decoded):
        anomalies.append(
            _anomaly(
                "entry_index_census",
                f"{field} has non-integer or negative entries: {values!r}",
                relationship_id=relationship_id,
            )
        )
        return None
    ints = [int(index) for index in decoded]  # type: ignore[arg-type]
    if ints != sorted(set(ints)):
        anomalies.append(
            _anomaly(
                "entry_index_census",
                f"{field} is not sorted/unique: {ints!r}",
                relationship_id=relationship_id,
            )
        )
        return None
    if n_compile_entries is not None and any(
        index >= n_compile_entries for index in ints
    ):
        anomalies.append(
            _anomaly(
                "entry_index_census",
                f"{field} {ints!r} outside manifest compile-entry census "
                f"(n={n_compile_entries})",
                relationship_id=relationship_id,
            )
        )
        return None
    return ints


def _validate_compiler_json(
    raw: Any,
    *,
    relationship_id: Optional[str],
    digest: Optional[str],
    manifest_compilers: Set[Tuple[str, str]],
    anomalies: List[Dict[str, Any]],
) -> Set[Tuple[str, str]]:
    decoded = _decoded_json_list(
        raw,
        relationship_id=relationship_id,
        field="clang_call_compilers_json",
        code="compilers_json",
        anomalies=anomalies,
    )
    identities: Set[Tuple[str, str]] = set()
    if decoded is None:
        return identities
    for position, compiler in enumerate(decoded):
        missing = [key for key in _COMPILER_JSON_KEYS if key not in compiler]
        if missing:
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    f"clang_call_compilers_json[{position}] missing {missing}",
                    relationship_id=relationship_id,
                )
            )
            continue
        path = compiler.get("compiler_path")
        cid = compiler.get("compiler_id")
        cdigest = compiler.get("compile_commands_digest")
        if (
            not isinstance(path, str)
            or not path.strip()
            or path != path.strip()
            or not Path(path).is_absolute()
        ):
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    f"clang_call_compilers_json[{position}] compiler_path is "
                    f"not a canonical absolute path: {path!r}",
                    relationship_id=relationship_id,
                )
            )
            continue
        if not isinstance(cid, str) or not cid.strip() or cid != cid.strip():
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    f"clang_call_compilers_json[{position}] compiler_id is "
                    f"not a nonempty canonical string: {cid!r}",
                    relationship_id=relationship_id,
                )
            )
            continue
        if digest is not None and cdigest != digest:
            anomalies.append(
                _anomaly(
                    "digest_mismatch",
                    f"clang_call_compilers_json[{position}] digest "
                    f"{cdigest!r} != {digest!r}",
                    relationship_id=relationship_id,
                )
            )
        identity = (path, cid)
        if identity in identities:
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    f"clang_call_compilers_json contains duplicate {identity!r}",
                    relationship_id=relationship_id,
                )
            )
            continue
        if manifest_compilers and identity not in manifest_compilers:
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    f"clang_call_compilers_json[{position}] identity absent "
                    "from the manifest census",
                    relationship_id=relationship_id,
                )
            )
            continue
        identities.add(identity)
    return identities


def _validate_observations(
    raw: Any,
    *,
    relationship_id: Optional[str],
    target: Any,
    ref_type: Any,
    digest: Optional[str],
    entry_indices: Optional[List[int]],
    compiler_identities: Set[Tuple[str, str]],
    manifest_compilers: Set[Tuple[str, str]],
    n_compile_entries: Optional[int],
    anomalies: List[Dict[str, Any]],
) -> None:
    decoded = _decoded_json_list(
        raw,
        relationship_id=relationship_id,
        field="clang_call_observations_json",
        code="observations_json",
        anomalies=anomalies,
    )
    if decoded is None:
        return
    observed: List[int] = []
    observed_compilers: Set[Tuple[str, str]] = set()
    required = (
        "classification",
        "entry_index",
        "target_title",
        "ref_kind",
        "ref_type",
        "compiler_path",
        "compiler_id",
        "compile_commands_digest",
    )
    for position, record in enumerate(decoded):
        missing = [field for field in required if field not in record]
        if missing:
            anomalies.append(
                _anomaly(
                    "observation_record",
                    f"observation[{position}] missing {missing}",
                    relationship_id=relationship_id,
                )
            )
            continue
        if record.get("classification") != "internal_direct":
            anomalies.append(
                _anomaly(
                    "observation_record",
                    f"observation[{position}] classification="
                    f"{record.get('classification')!r} expected "
                    "'internal_direct'",
                    relationship_id=relationship_id,
                )
            )
        entry_index = _as_strict_int(record.get("entry_index"))
        if (
            entry_index is None
            or entry_index < 0
            or (
                n_compile_entries is not None
                and entry_index >= n_compile_entries
            )
        ):
            anomalies.append(
                _anomaly(
                    "observation_record",
                    f"observation[{position}] entry_index="
                    f"{record.get('entry_index')!r} is outside the compile "
                    "census",
                    relationship_id=relationship_id,
                )
            )
        else:
            observed.append(entry_index)
        if record.get("target_title") != target:
            anomalies.append(
                _anomaly(
                    "observation_record",
                    f"observation[{position}] target_title="
                    f"{record.get('target_title')!r} != relationship.target "
                    f"{target!r}",
                    relationship_id=relationship_id,
                )
            )
        if record.get("ref_kind") != "FunctionDecl":
            anomalies.append(
                _anomaly(
                    "observation_record",
                    f"observation[{position}] ref_kind="
                    f"{record.get('ref_kind')!r} expected 'FunctionDecl'",
                    relationship_id=relationship_id,
                )
            )
        obs_ref_type = record.get("ref_type")
        if not is_material_value(obs_ref_type):
            obs_ref_type = None
        expected_ref = ref_type if is_material_value(ref_type) else None
        if obs_ref_type != expected_ref:
            anomalies.append(
                _anomaly(
                    "observation_record",
                    f"observation[{position}] ref_type={obs_ref_type!r} != "
                    f"clang_call_ref_type {expected_ref!r}",
                    relationship_id=relationship_id,
                )
            )
        path = record.get("compiler_path")
        cid = record.get("compiler_id")
        odigest = record.get("compile_commands_digest")
        if (
            not isinstance(path, str)
            or not path.strip()
            or path != path.strip()
            or not Path(path).is_absolute()
            or not isinstance(cid, str)
            or not cid.strip()
            or cid != cid.strip()
        ):
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    f"observation[{position}] has a non-canonical compiler "
                    "identity",
                    relationship_id=relationship_id,
                )
            )
        else:
            identity = (path, cid)
            observed_compilers.add(identity)
            if compiler_identities and identity not in compiler_identities:
                anomalies.append(
                    _anomaly(
                        "compiler_mismatch",
                        f"observation[{position}] compiler identity is absent "
                        "from clang_call_compilers_json",
                        relationship_id=relationship_id,
                    )
                )
            if manifest_compilers and identity not in manifest_compilers:
                anomalies.append(
                    _anomaly(
                        "compiler_mismatch",
                        f"observation[{position}] compiler identity is absent "
                        "from the manifest census",
                        relationship_id=relationship_id,
                    )
                )
        if digest is not None and odigest != digest:
            anomalies.append(
                _anomaly(
                    "digest_mismatch",
                    f"observation[{position}] digest {odigest!r} != {digest!r}",
                    relationship_id=relationship_id,
                )
            )
    if entry_indices is not None:
        if sorted(observed) != entry_indices or len(observed) != len(set(observed)):
            anomalies.append(
                _anomaly(
                    "observation_coverage",
                    f"observation entry indices {sorted(observed)!r} do not "
                    f"cover clang_call_entry_indices {entry_indices!r} "
                    "exactly",
                    relationship_id=relationship_id,
                )
            )
    if compiler_identities and observed_compilers != compiler_identities:
        anomalies.append(
            _anomaly(
                "compiler_mismatch",
                "observation compiler identities != clang_call_compilers_json",
                relationship_id=relationship_id,
            )
        )


def _validate_decorated_relationship(
    row: Any,
    *,
    relationship_id: Optional[str],
    manifest_digest: Optional[str],
    manifest_compilers: Set[Tuple[str, str]],
    n_compile_entries: Optional[int],
    anomalies: List[Dict[str, Any]],
) -> None:
    present = set(_row_keys(row))
    missing = [field for field in _CALL_FIELDS if field not in present]
    if missing:
        anomalies.append(
            _anomaly(
                "partial_call_payload",
                f"decorated relationship is missing required keys: {missing}",
                relationship_id=relationship_id,
            )
        )
    unknown = sorted(
        key
        for key in present
        if key.startswith("clang_call_")
        and key not in _CALL_FIELDS
        and is_material_value(_row_get(row, key))
    )
    if unknown:
        anomalies.append(
            _anomaly(
                "unknown_call_field",
                f"unknown clang_call_* fields: {unknown}",
                relationship_id=relationship_id,
            )
        )

    for field in ("source", "target", "source_file", "span"):
        value = _row_get(row, field)
        if not isinstance(value, str) or not value.strip():
            anomalies.append(
                _anomaly(
                    "call_field_type",
                    f"decorated relationship {field} is empty",
                    relationship_id=relationship_id,
                )
            )

    if _row_get(row, "clang_call_status") != "matched":
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_call_status={_row_get(row, 'clang_call_status')!r} "
                "expected 'matched'",
                relationship_id=relationship_id,
            )
        )
    if _row_get(row, "clang_call_fact_kind") != FACT_KIND:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_call_fact_kind="
                f"{_row_get(row, 'clang_call_fact_kind')!r} expected "
                f"{FACT_KIND!r}",
                relationship_id=relationship_id,
            )
        )
    if _row_get(row, "clang_call_extractor") != EXTRACTOR:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_call_extractor="
                f"{_row_get(row, 'clang_call_extractor')!r} expected "
                f"{EXTRACTOR!r}",
                relationship_id=relationship_id,
            )
        )
    if not _is_one(_row_get(row, "clang_call_confidence")):
        anomalies.append(
            _anomaly(
                "call_field_type",
                f"clang_call_confidence="
                f"{_row_get(row, 'clang_call_confidence')!r} expected 1.0",
                relationship_id=relationship_id,
            )
        )
    if _as_bool(_row_get(row, "clang_call_is_deterministic")) is not True:
        anomalies.append(
            _anomaly(
                "call_field_type",
                "clang_call_is_deterministic is not boolean true",
                relationship_id=relationship_id,
            )
        )
    match_basis = _row_get(row, "clang_call_match_basis")
    if match_basis not in _MATCH_BASES:
        anomalies.append(
            _anomaly(
                "match_basis",
                f"clang_call_match_basis={match_basis!r} is not "
                "exact_byte_offset or exact_line_col_fallback",
                relationship_id=relationship_id,
            )
        )
    byte_offset = _as_int(_row_get(row, "clang_call_byte_offset"))
    if byte_offset is None or byte_offset < 0:
        anomalies.append(
            _anomaly(
                "byte_offset",
                f"clang_call_byte_offset="
                f"{_row_get(row, 'clang_call_byte_offset')!r} is not a "
                "non-negative integer",
                relationship_id=relationship_id,
            )
        )
    if _row_get(row, "clang_call_ref_kind") != "FunctionDecl":
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_call_ref_kind={_row_get(row, 'clang_call_ref_kind')!r} "
                "expected 'FunctionDecl'",
                relationship_id=relationship_id,
            )
        )
    resolve_reason = _row_get(row, "clang_call_resolve_reason")
    if is_material_value(resolve_reason) and not isinstance(resolve_reason, str):
        anomalies.append(
            _anomaly(
                "call_field_type",
                f"clang_call_resolve_reason must be a string or null: "
                f"{resolve_reason!r}",
                relationship_id=relationship_id,
            )
        )
    ref_type = _row_get(row, "clang_call_ref_type")
    if is_material_value(ref_type) and not isinstance(ref_type, str):
        anomalies.append(
            _anomaly(
                "call_field_type",
                f"clang_call_ref_type must be a string or null: {ref_type!r}",
                relationship_id=relationship_id,
            )
        )

    digest = _row_get(row, "clang_call_compile_commands_digest")
    if not isinstance(digest, str) or not digest.strip() or digest != digest.strip():
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"clang_call_compile_commands_digest is not a nonempty "
                f"canonical string: {digest!r}",
                relationship_id=relationship_id,
            )
        )
        digest = None
    elif manifest_digest is not None and digest != manifest_digest:
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"relationship digest {digest!r} != manifest "
                f"{manifest_digest!r}",
                relationship_id=relationship_id,
            )
        )

    entry_indices = _decoded_indices(
        _row_get(row, "clang_call_entry_indices"),
        relationship_id=relationship_id,
        field="clang_call_entry_indices",
        n_compile_entries=n_compile_entries,
        anomalies=anomalies,
    )
    compiler_identities = _validate_compiler_json(
        _row_get(row, "clang_call_compilers_json"),
        relationship_id=relationship_id,
        digest=digest,
        manifest_compilers=manifest_compilers,
        anomalies=anomalies,
    )
    singular_path = _row_get(row, "clang_call_compiler_path")
    singular_id = _row_get(row, "clang_call_compiler_id")
    has_singular = is_material_value(singular_path) or is_material_value(
        singular_id
    )
    if len(compiler_identities) == 1:
        only = next(iter(compiler_identities))
        if (
            not isinstance(singular_path, str)
            or not isinstance(singular_id, str)
            or (singular_path, singular_id) != only
        ):
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    "singular compiler fields disagree with "
                    "clang_call_compilers_json",
                    relationship_id=relationship_id,
                )
            )
    elif len(compiler_identities) > 1 and has_singular:
        anomalies.append(
            _anomaly(
                "compiler_mismatch",
                "multi-compiler relationship exposes singular compiler fields",
                relationship_id=relationship_id,
            )
        )

    _validate_observations(
        _row_get(row, "clang_call_observations_json"),
        relationship_id=relationship_id,
        target=_row_get(row, "target"),
        ref_type=ref_type,
        digest=digest,
        entry_indices=entry_indices,
        compiler_identities=compiler_identities,
        manifest_compilers=manifest_compilers,
        n_compile_entries=n_compile_entries,
        anomalies=anomalies,
    )

    description = _row_get(row, "clang_call_description")
    source = _row_get(row, "source")
    target = _row_get(row, "target")
    if not isinstance(description, str) or not description:
        anomalies.append(
            _anomaly(
                "description_mismatch",
                "clang_call_description is empty",
                relationship_id=relationship_id,
            )
        )
    elif (
        isinstance(source, str)
        and isinstance(target, str)
        and match_basis in _MATCH_BASES
    ):
        expected = _producer_description(source, target, str(match_basis))
        if description != expected:
            anomalies.append(
                _anomaly(
                    "description_mismatch",
                    "clang_call_description does not match the producer format",
                    relationship_id=relationship_id,
                )
            )


def _validate_call_manifest_block(
    block: Dict[str, Any],
    *,
    n_decorated: int,
    n_calls: int,
    anomalies: List[Dict[str, Any]],
) -> Tuple[Optional[str], Set[Tuple[str, str]], Optional[int], Dict[str, int]]:
    extra = sorted(
        repr(key) for key in set(block) - set(_ENABLED_MANIFEST_KEYS)
    )
    missing = [key for key in _ENABLED_MANIFEST_KEYS if key not in block]
    if extra:
        anomalies.append(
            _anomaly(
                "extra_manifest_key",
                f"enabled clang_calls block has extra keys: {extra}",
            )
        )
    if missing:
        anomalies.append(
            _anomaly(
                "missing_manifest_key",
                f"enabled clang_calls block is missing keys: {missing}",
            )
        )
    if block.get("mode") != MODE:
        anomalies.append(
            _anomaly(
                "manifest_mode_mismatch",
                f"enabled block mode={block.get('mode')!r} expected {MODE!r}",
            )
        )
    if block.get("enabled") is not True:
        anomalies.append(
            _anomaly(
                "manifest_mode_mismatch",
                f"enabled block enabled={block.get('enabled')!r} expected True",
            )
        )
    for field, expected in (("fact_kind", FACT_KIND), ("extractor", EXTRACTOR)):
        if block.get(field) != expected:
            anomalies.append(
                _anomaly(
                    "manifest_identity_mismatch",
                    f"manifest {field}={block.get(field)!r} expected {expected!r}",
                )
            )

    counts_raw = block.get("counts")
    counts: Dict[str, int] = {}
    if not isinstance(counts_raw, dict):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"manifest counts is not an object: {type(counts_raw).__name__}",
            )
        )
    else:
        extra_counts = sorted(
            repr(key) for key in set(counts_raw) - set(_COUNT_KEYS)
        )
        if extra_counts:
            anomalies.append(
                _anomaly(
                    "extra_count_key",
                    f"manifest counts has extra keys: {extra_counts}",
                )
            )
        for key in _COUNT_KEYS:
            value = counts_raw.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                anomalies.append(
                    _anomaly(
                        "manifest_count_mismatch",
                        f"manifest counts.{key}={value!r} is not a "
                        "non-negative integer",
                    )
                )
            else:
                counts[key] = value
        for key in _FAIL_CLOSED_COUNT_KEYS:
            if counts.get(key):
                anomalies.append(
                    _anomaly(
                        "residual_bucket_nonzero",
                        f"fail-closed residual counts.{key}={counts[key]} "
                        "must be zero in a published overlay",
                    )
                )
        if "matched_internal" in counts and counts["matched_internal"] != n_decorated:
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"manifest counts.matched_internal="
                    f"{counts['matched_internal']} != {n_decorated} decorated "
                    "relationships",
                )
            )

    n_facts = block.get("n_facts")
    if (
        isinstance(n_facts, bool)
        or not isinstance(n_facts, int)
        or n_facts != n_decorated
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"manifest n_facts={n_facts!r} != {n_decorated} decorated "
                "relationships",
            )
        )
    n_changed = block.get("n_facts_changed")
    upper = (
        n_facts
        if isinstance(n_facts, int) and not isinstance(n_facts, bool)
        else n_decorated
    )
    if (
        isinstance(n_changed, bool)
        or not isinstance(n_changed, int)
        or n_changed < 0
        or n_changed > upper
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"manifest n_facts_changed={n_changed!r} is not within "
                f"[0, {upper}]",
            )
        )

    n_compile_entries = block.get("n_compile_entries")
    n_translation_units = block.get("n_translation_units")
    if (
        isinstance(n_compile_entries, bool)
        or not isinstance(n_compile_entries, int)
        or n_compile_entries <= 0
        or isinstance(n_translation_units, bool)
        or not isinstance(n_translation_units, int)
        or n_translation_units != n_compile_entries
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"compile-entry/TU census is invalid: "
                f"n_compile_entries={n_compile_entries!r} "
                f"n_translation_units={n_translation_units!r}",
            )
        )
        n_compile_entries = None

    accounting = block.get("tree_sitter_accounting")
    if not isinstance(accounting, dict):
        anomalies.append(
            _anomaly(
                "accounting_mismatch",
                f"tree_sitter_accounting is not an object: "
                f"{type(accounting).__name__}",
            )
        )
    else:
        extra_acc = sorted(
            repr(key) for key in set(accounting) - set(_ACCOUNTING_KEYS)
        )
        if extra_acc:
            anomalies.append(
                _anomaly(
                    "extra_accounting_key",
                    f"tree_sitter_accounting has extra keys: {extra_acc}",
                )
            )
        acc_vals: Dict[str, int] = {}
        for key in _ACCOUNTING_KEYS:
            value = accounting.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                anomalies.append(
                    _anomaly(
                        "accounting_mismatch",
                        f"tree_sitter_accounting.{key}={value!r} is not a "
                        "non-negative integer",
                    )
                )
            else:
                acc_vals[key] = value
        if acc_vals.get("total_calls") != n_calls:
            anomalies.append(
                _anomaly(
                    "accounting_mismatch",
                    f"tree_sitter_accounting.total_calls="
                    f"{acc_vals.get('total_calls')!r} != {n_calls} calls "
                    "relationships",
                )
            )
        if {
            "total_calls",
            "matched_internal",
            "covered_by_noninternal_clang_observation",
            "tree_sitter_only_internal",
            "out_of_compile_db_scope",
        } <= set(acc_vals):
            parts = (
                acc_vals["matched_internal"]
                + acc_vals["covered_by_noninternal_clang_observation"]
                + acc_vals["tree_sitter_only_internal"]
                + acc_vals["out_of_compile_db_scope"]
            )
            if parts != acc_vals["total_calls"]:
                anomalies.append(
                    _anomaly(
                        "accounting_mismatch",
                        "tree_sitter_accounting components do not sum to "
                        f"total_calls: {parts} != {acc_vals['total_calls']}",
                    )
                )
        if (
            "matched_internal" in acc_vals
            and "matched_internal" in counts
            and acc_vals["matched_internal"] != counts["matched_internal"]
        ):
            anomalies.append(
                _anomaly(
                    "accounting_mismatch",
                    "tree_sitter_accounting.matched_internal disagrees with "
                    "counts.matched_internal",
                )
            )
        if (
            "tree_sitter_only_internal" in acc_vals
            and "tree_sitter_only_internal" in counts
            and acc_vals["tree_sitter_only_internal"]
            != counts["tree_sitter_only_internal"]
        ):
            anomalies.append(
                _anomaly(
                    "accounting_mismatch",
                    "tree_sitter_accounting.tree_sitter_only_internal "
                    "disagrees with counts.tree_sitter_only_internal",
                )
            )
        if (
            "out_of_compile_db_scope" in acc_vals
            and "out_of_compile_db_scope" in counts
            and acc_vals["out_of_compile_db_scope"]
            != counts["out_of_compile_db_scope"]
        ):
            anomalies.append(
                _anomaly(
                    "accounting_mismatch",
                    "tree_sitter_accounting.out_of_compile_db_scope disagrees "
                    "with counts.out_of_compile_db_scope",
                )
            )
        if acc_vals.get("covered_by_noninternal_clang_observation"):
            anomalies.append(
                _anomaly(
                    "residual_bucket_nonzero",
                    "fail-closed residual "
                    "tree_sitter_accounting.covered_by_noninternal_clang_"
                    f"observation="
                    f"{acc_vals['covered_by_noninternal_clang_observation']} "
                    "must be zero in a published overlay",
                )
            )

    raw_digest = block.get("compile_commands_digest")
    digest: Optional[str] = None
    if (
        not isinstance(raw_digest, str)
        or not raw_digest.strip()
        or raw_digest != raw_digest.strip()
    ):
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"manifest compile_commands_digest missing: {raw_digest!r}",
            )
        )
    else:
        digest = raw_digest

    compilers_block = block.get("compilers")
    identities: Set[Tuple[str, str]] = set()
    valid_compilers: List[Dict[str, Any]] = []
    if (
        not isinstance(compilers_block, list)
        or not compilers_block
        or not all(isinstance(item, dict) for item in compilers_block)
        or _contains_non_finite(compilers_block)
    ):
        anomalies.append(
            _anomaly(
                "manifest_identity_mismatch",
                "manifest compilers must be a non-empty finite list of objects",
            )
        )
    else:
        for position, compiler in enumerate(compilers_block):
            compiler_keys = set(compiler)
            missing_compiler_keys = [
                key for key in _MANIFEST_COMPILER_KEYS if key not in compiler
            ]
            extra_compiler_keys = sorted(
                repr(key)
                for key in compiler_keys - set(_MANIFEST_COMPILER_KEYS)
            )
            if missing_compiler_keys or extra_compiler_keys:
                anomalies.append(
                    _anomaly(
                        "manifest_identity_mismatch",
                        f"manifest compilers[{position}] key set differs "
                        "from the producer contract: "
                        f"missing={missing_compiler_keys}, "
                        f"extra={extra_compiler_keys}",
                    )
                )
            path = compiler.get("compiler_path")
            cid = compiler.get("compiler_id")
            version = compiler.get("compiler_version")
            if (
                not isinstance(path, str)
                or not path.strip()
                or path != path.strip()
                or not Path(path).is_absolute()
                or not isinstance(cid, str)
                or not cid.strip()
                or cid != cid.strip()
            ):
                anomalies.append(
                    _anomaly(
                        "manifest_identity_mismatch",
                        f"manifest compilers[{position}] has incomplete "
                        "identity",
                    )
                )
                continue
            if version is not None and (
                not isinstance(version, str)
                or not version.strip()
                or version != version.strip()
            ):
                anomalies.append(
                    _anomaly(
                        "manifest_identity_mismatch",
                        f"manifest compilers[{position}] has invalid "
                        f"compiler_version={version!r}",
                    )
                )
            identity = (path, cid)
            if identity in identities:
                anomalies.append(
                    _anomaly(
                        "manifest_identity_mismatch",
                        f"manifest compilers contains duplicate {identity!r}",
                    )
                )
                continue
            identities.add(identity)
            valid_compilers.append(compiler)
        ordered = sorted(
            compilers_block,
            key=lambda item: (
                str(item.get("compiler_path") or ""),
                str(item.get("compiler_id") or ""),
            ),
        )
        if compilers_block != ordered:
            anomalies.append(
                _anomaly(
                    "manifest_identity_mismatch",
                    "manifest compilers is not in producer canonical order",
                )
            )

    if len(valid_compilers) == 1:
        only = valid_compilers[0]
        for field in ("compiler_path", "compiler_id"):
            if block.get(field) != only.get(field):
                anomalies.append(
                    _anomaly(
                        "manifest_identity_mismatch",
                        f"manifest {field}={block.get(field)!r} != "
                        f"compilers[0].{field}={only.get(field)!r}",
                    )
                )
        if block.get("compiler_version") != only.get("compiler_version"):
            anomalies.append(
                _anomaly(
                    "manifest_identity_mismatch",
                    "manifest compiler_version disagrees with compilers[0]",
                )
            )
    elif len(valid_compilers) > 1:
        for field in ("compiler_path", "compiler_id", "compiler_version"):
            if block.get(field) is not None:
                anomalies.append(
                    _anomaly(
                        "manifest_identity_mismatch",
                        f"multi-compiler manifest exposes singular "
                        f"{field}={block.get(field)!r}",
                    )
                )

    if block.get("confidence_boundary") != CONFIDENCE_BOUNDARY:
        anomalies.append(
            _anomaly(
                "manifest_contract_claim",
                "manifest confidence_boundary differs from the producer contract",
            )
        )
    return digest, identities, n_compile_entries, counts


def validate_persisted_call_overlay(
    relationships: Any,
    manifest: Optional[Any] = None,
    *,
    max_anomaly_samples: int = _MAX_ANOMALY_SAMPLES,
) -> Dict[str, Any]:
    """Validate already-persisted Clang configured-call relationship evidence.

    Pure and non-mutating. Never invokes Clang, loads compile_commands.json,
    reads C sources, reconstructs byte offsets, reindexes, or repairs rows.
    """
    if max_anomaly_samples < 0:
        max_anomaly_samples = 0

    relationships_list = _table_rows(relationships, name="relationships")
    manifest_obj: Dict[str, Any] = {}
    if manifest is not None:
        if isinstance(manifest, dict):
            manifest_obj = manifest
        elif hasattr(manifest, "items"):
            manifest_obj = dict(manifest)
        else:
            raise TypeError("manifest must be a mapping or None")

    anomalies: List[Dict[str, Any]] = []

    seen_ids: Dict[str, int] = {}
    for index, row in enumerate(relationships_list):
        raw_id = _row_get(row, "id")
        if not is_material_value(raw_id) or not str(raw_id).strip():
            anomalies.append(
                _anomaly(
                    "empty_relationship_id",
                    f"relationship at index {index} has empty id",
                )
            )
            continue
        relationship_id = str(raw_id)
        if relationship_id in seen_ids:
            anomalies.append(
                _anomaly(
                    "duplicate_relationship_id",
                    f"duplicate relationship id {relationship_id!r}",
                    relationship_id=relationship_id,
                    extra={"other_index": seen_ids[relationship_id]},
                )
            )
        else:
            seen_ids[relationship_id] = index

    n_calls = 0
    carrying: List[Dict[str, Any]] = []
    decorated: List[Dict[str, Any]] = []
    for row in relationships_list:
        if str(_row_get(row, "type") or "") == "calls":
            n_calls += 1
        if not _has_material_call_fields(row):
            continue
        carrying.append(row)
        relationship_id = str(_row_get(row, "id") or "") or None
        if str(_row_get(row, "type") or "") == "calls":
            decorated.append(row)
        else:
            anomalies.append(
                _anomaly(
                    "stale_call_metadata",
                    f"non-calls relationship carries clang_call_* fields "
                    f"(type={_row_get(row, 'type')!r})",
                    relationship_id=relationship_id,
                )
            )

    has_block = "clang_calls" in manifest_obj
    block = manifest_obj.get("clang_calls")
    mode_state = "legacy_absent"
    block_enabled = False

    if not has_block:
        if carrying:
            anomalies.append(
                _anomaly(
                    "legacy_block_missing_with_fields",
                    f"manifest lacks clang_calls but graph has "
                    f"{len(carrying)} relationship(s) with clang_call_* fields",
                    extra={"n_relationships": len(carrying)},
                )
            )
            mode_state = "invalid"
    elif not isinstance(block, dict):
        anomalies.append(
            _anomaly(
                "invalid_enabled_block",
                f"clang_calls manifest block is not an object: "
                f"{type(block).__name__}",
            )
        )
        mode_state = "invalid"
    else:
        mode = block.get("mode")
        enabled = block.get("enabled")
        if mode == "off" and enabled is False:
            mode_state = "off"
            extra = sorted(
                repr(key) for key in set(block) - set(_OFF_MANIFEST_KEYS)
            )
            missing = [key for key in _OFF_MANIFEST_KEYS if key not in block]
            if extra:
                anomalies.append(
                    _anomaly(
                        "extra_manifest_key",
                        f"off clang_calls block has extra keys: {extra}",
                    )
                )
            if missing:
                anomalies.append(
                    _anomaly(
                        "missing_manifest_key",
                        f"off clang_calls block is missing keys: {missing}",
                    )
                )
            if carrying:
                anomalies.append(
                    _anomaly(
                        "off_with_decorated_relationships",
                        f"clang_calls is off/disabled but graph has "
                        f"{len(carrying)} relationship(s) with clang_call_* "
                        "fields",
                        extra={"n_relationships": len(carrying)},
                    )
                )
            disabled_counts = {
                key: block.get(key) for key in ("n_facts", "n_translation_units")
            }
            if any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value != 0
                for value in disabled_counts.values()
            ):
                anomalies.append(
                    _anomaly(
                        "manifest_count_mismatch",
                        f"off block has invalid zero census {disabled_counts!r}",
                    )
                )
        elif mode == MODE and enabled is True:
            mode_state = "enabled"
            block_enabled = True
        else:
            anomalies.append(
                _anomaly(
                    "invalid_enabled_block",
                    f"clang_calls enablement inconsistent: mode={mode!r} "
                    f"enabled={enabled!r}",
                )
            )
            mode_state = "invalid"
            block_enabled = bool(enabled) or mode == MODE

    manifest_digest: Optional[str] = None
    manifest_compilers: Set[Tuple[str, str]] = set()
    n_compile_entries: Optional[int] = None
    counts: Dict[str, int] = {}
    if block_enabled and isinstance(block, dict):
        (
            manifest_digest,
            manifest_compilers,
            n_compile_entries,
            counts,
        ) = _validate_call_manifest_block(
            block,
            n_decorated=len(decorated),
            n_calls=n_calls,
            anomalies=anomalies,
        )

    if block_enabled or (mode_state == "invalid" and carrying):
        for row in sorted(
            decorated,
            key=lambda item: (
                str(_row_get(item, "source") or ""),
                str(_row_get(item, "target") or ""),
                str(_row_get(item, "id") or ""),
            ),
        ):
            _validate_decorated_relationship(
                row,
                relationship_id=str(_row_get(row, "id") or "") or None,
                manifest_digest=manifest_digest,
                manifest_compilers=manifest_compilers,
                n_compile_entries=n_compile_entries,
                anomalies=anomalies,
            )

    anomalies.sort(
        key=lambda item: (
            str(item.get("code") or ""),
            str(item.get("relationship_id") or ""),
            str(item.get("message") or ""),
            _strict_canonical_json(
                {
                    key: item[key]
                    for key in sorted(item)
                    if key not in {"code", "message", "relationship_id"}
                }
            ),
        )
    )
    total = len(anomalies)
    samples = anomalies[:max_anomaly_samples]
    ok = total == 0 and mode_state in {"legacy_absent", "off", "enabled"}
    status = mode_state if ok else "invalid"
    return {
        "ok": ok,
        "status": status,
        "mode": mode_state,
        "n_relationships": len(relationships_list),
        "n_calls": n_calls,
        "n_decorated_relationships": len(decorated),
        "n_call_field_carriers": len(carrying),
        "n_anomalies": total,
        "n_anomaly_samples": len(samples),
        "anomalies_truncated": total > len(samples),
        "anomalies": samples,
        "counts": dict(sorted(counts.items())),
        "provenance": {
            "compile_commands_digest": manifest_digest,
            "n_compile_entries": n_compile_entries,
            "compilers": [
                {"compiler_path": path, "compiler_id": cid}
                for path, cid in sorted(manifest_compilers)
            ],
        },
        "overlay_mode": MODE,
        "fact_kind": FACT_KIND,
        "extractor": EXTRACTOR,
        "limitations": list(LIMITATIONS),
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }
