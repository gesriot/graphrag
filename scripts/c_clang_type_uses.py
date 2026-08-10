#!/usr/bin/env python
"""Optional configured Clang type-use graph overlay (``uses_type`` edges).

Publishes aggregated ``uses_type`` relationships from the diagnostic type-use
audit’s ``matched_internal`` rows only. Does **not** reimplement AST extraction
or matching — that remains exclusively in ``c_clang_type_use_audit``.

Aggregation key is the existing tree-sitter **entity id** pair
(source owner, target type). Multiple observations for the same pair become
one relationship with deterministic evidence JSON.

This is configuration/toolchain-derived relative to the recorded Clang +
compile_commands.json only — not ABI/layout/multi-config/points-to proof.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from c_clang_type_use_audit import (  # type: ignore
    ClangTypeUseAuditError,
    run_clang_type_use_audit,
)
from c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    next_human_readable_id,
    path_is_under,
)
from c_identities import package_relative_posix  # type: ignore

MODE = "configured_clang_type_uses"
FACT_KIND = "configured_type_use"
EXTRACTOR = "clang-ast-json"
REL_TYPE = "uses_type"
CONFIDENCE_BOUNDARY = (
    "clang_type_use_confidence=1.0 and clang_type_use_is_deterministic=true "
    "mean each uses_type edge is re-derivable from the recorded Clang + "
    "compile_commands.json configuration via the type-use audit's "
    "matched_internal observations only. This is not layout/ABI proof, not "
    "multi-config coverage, not points-to analysis, not C++, and not an "
    "exact type-token location claim. Residuals "
    "(owner_unmatched/target_unresolved/ambiguous_target/"
    "macro_location_unsupported) fail closed; external_or_system, "
    "unsupported_type_form, and unowned_context remain observation-only "
    "without invented edges."
)

_FAIL_CLOSED_BUCKETS = (
    "owner_unmatched",
    "target_unresolved",
    "ambiguous_target",
    "macro_location_unsupported",
)

_OBSERVATION_ONLY_BUCKETS = (
    "external_or_system",
    "unsupported_type_form",
    "unowned_context",
)

_ALL_BUCKETS = (
    "matched_internal",
    *_FAIL_CLOSED_BUCKETS,
    *_OBSERVATION_ONLY_BUCKETS,
)

_OWNER_KINDS = frozenset({"function", "struct", "enum", "typedef"})
_TARGET_KINDS = frozenset({"struct", "enum", "typedef"})
_USE_KINDS = frozenset(
    {
        "function_return",
        "parameter",
        "local_variable",
        "field",
        "typedef_underlying",
    }
)
_TARGET_RESOLVERS = frozenset(
    {"type_alias_decl_id", "exact_tag_spelling", "unique_typedef_spelling"}
)
_OWNER_RESOLVERS = frozenset(
    {
        "exact_declaration_site",
        "unique_internal_function_name_same_file",
        "unique_external_function_name",
        "owned_tag_typedef_site",
    }
)

# Known material clang_type_use_* field names produced by this overlay.
# Unknown material clang_type_use_* keys on configured edges fail closed.
TYPE_USE_FIELDS: Tuple[str, ...] = (
    "clang_type_use_status",
    "clang_type_use_fact_kind",
    "clang_type_use_extractor",
    "clang_type_use_confidence",
    "clang_type_use_is_deterministic",
    "clang_type_use_observation_count",
    "clang_type_use_use_kinds",
    "clang_type_use_entry_indices",
    "clang_type_use_compiler_path",
    "clang_type_use_compiler_id",
    "clang_type_use_compilers",
    "clang_type_use_compile_commands_digest",
    "clang_type_use_observations_json",
    "clang_type_use_source_entity_id",
    "clang_type_use_target_entity_id",
    "clang_type_use_description",
)
# Private alias retained for in-module call sites and existing imports.
_TYPE_USE_FIELDS = TYPE_USE_FIELDS

# Stable anomaly codes for the read-only persisted integrity audit.
ANOMALY_CODES = (
    "legacy_block_missing_with_edges",
    "off_with_configured_edges",
    "duplicate_relationship_id",
    "empty_relationship_id",
    "stale_type_use_metadata",
    "non_configured_uses_type",
    "field_mismatch",
    "confidence_boundary",
    "dangling_source",
    "dangling_target",
    "ambiguous_source_title",
    "ambiguous_target_title",
    "entity_id_mismatch",
    "invalid_target_kind",
    "invalid_source_kind",
    "relationship_id_mismatch",
    "duplicate_endpoint_pair",
    "invalid_human_readable_id",
    "unknown_type_use_field",
    "malformed_observations_json",
    "non_object_observation",
    "invalid_observation",
    "empty_observations",
    "observation_count_mismatch",
    "non_canonical_observation_order",
    "use_kinds_mismatch",
    "entry_indices_mismatch",
    "malformed_compilers_json",
    "compiler_shortcut_mismatch",
    "digest_mismatch",
    "manifest_mode_mismatch",
    "manifest_count_mismatch",
    "manifest_identity_mismatch",
    "nan_or_infinity",
    "invalid_enabled_block",
)


class ClangTypeUseOverlayError(CompilerOverlayError):
    """Raised when the uses_type overlay cannot apply honestly."""


def build_disabled_provenance() -> Dict[str, Any]:
    """Stable manifest block when ``--clang-type-uses`` is off."""
    return {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_observations": 0,
        "n_translation_units": 0,
    }


def canonical_json(value: Any) -> str:
    """Canonical JSON encoding used by producer evidence aggregation."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _canonical_json(value: Any) -> str:
    """Private alias — producer call sites keep the underscore name."""
    return canonical_json(value)


def relationship_id(
    source_title: str,
    target_title: str,
    source_entity_id: str,
    target_entity_id: str,
) -> str:
    """Deterministic uses_type relationship id (producer contract).

    Public for integrity audits and tests. Must stay byte-identical to the
    historical private helper.
    """
    slug_src = re.sub(r"[^0-9A-Za-z_.]", "_", source_title)
    slug_tgt = re.sub(r"[^0-9A-Za-z_.]", "_", target_title)
    edge_digest = hashlib.sha256(
        (
            f"{source_entity_id}\0{target_entity_id}\0{source_title}\0"
            f"{target_title}\0{FACT_KIND}"
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"rel:uses_type:{slug_src}->{slug_tgt}:{edge_digest}"


def _relationship_id(
    source_title: str,
    target_title: str,
    source_entity_id: str,
    target_entity_id: str,
) -> str:
    """Private alias preserved for existing producer/tests imports."""
    return relationship_id(
        source_title, target_title, source_entity_id, target_entity_id
    )


def is_material_value(value: Any) -> bool:
    """True when a parquet/JSON cell is a real material value (not null/NaN)."""
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    # pandas/numpy NA
    try:
        import pandas as pd  # local: optional for pure-dict unit tests

        if value is pd.NA:
            return False
    except Exception:
        pass
    return True


def _contains_non_finite(value: Any) -> bool:
    """Return True for a nested NaN/Infinity without coercing the value."""
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


def _strict_json_loads(text: str) -> Any:
    """Decode standards-compliant JSON and reject duplicate object keys."""

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
        text,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )


def _is_finite_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return False
    return True


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


def _entry_indices(value: Any, *, context: str) -> List[int]:
    if not isinstance(value, list) or not value:
        raise ClangTypeUseOverlayError(
            f"{context} must have a non-empty list entry_indices"
        )
    out: Set[int] = set()
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ClangTypeUseOverlayError(
                f"{context} has invalid compile entry index {raw!r}"
            )
        if raw in out:
            raise ClangTypeUseOverlayError(
                f"{context} contains duplicate entry indices"
            )
        out.add(raw)
    return sorted(out)


def _index_entities(
    data: Dict[str, List[Dict[str, Any]]],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, Dict[str, Any]]]:
    by_title: Dict[str, List[Dict[str, Any]]] = {}
    by_id: Dict[str, Dict[str, Any]] = {}
    entities = data.get("entities")
    if not isinstance(entities, list) or not all(
        isinstance(e, dict) for e in entities
    ):
        raise ClangTypeUseOverlayError(
            "data.entities must be a list of objects"
        )
    for e in entities:
        if not isinstance(e, dict):
            continue
        title = str(e.get("title") or "")
        entity_id = str(e.get("id") or "")
        if not title or not entity_id:
            raise ClangTypeUseOverlayError(
                "every graph entity must have a non-empty title and id"
            )
        by_title.setdefault(title, []).append(e)
        if entity_id in by_id:
            raise ClangTypeUseOverlayError(
                f"duplicate graph entity id {entity_id!r}"
            )
        by_id[entity_id] = e
    return by_title, by_id


def _validated_buckets(
    report: Dict[str, Any],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    counts_raw = report.get("counts")
    if not isinstance(counts_raw, dict):
        raise ClangTypeUseOverlayError("type-use report has no counts object")
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    counts: Dict[str, int] = {}
    for key in _ALL_BUCKETS:
        rows = report.get(key)
        if not isinstance(rows, list) or not all(
            isinstance(r, dict) for r in rows
        ):
            raise ClangTypeUseOverlayError(
                f"type-use report bucket {key!r} must be a list of objects"
            )
        raw_count = counts_raw.get(key)
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 0
        ):
            raise ClangTypeUseOverlayError(
                f"type-use report count {key!r} must be a non-negative integer"
            )
        if raw_count != len(rows):
            raise ClangTypeUseOverlayError(
                f"type-use report count/list mismatch for {key!r}: "
                f"count={raw_count} rows={len(rows)}"
            )
        buckets[key] = list(rows)
        counts[key] = raw_count
    deduped_total = counts_raw.get("type_uses_deduped_total")
    raw_total = counts_raw.get("type_uses_raw_observations")
    if (
        isinstance(deduped_total, bool)
        or not isinstance(deduped_total, int)
        or deduped_total != sum(counts.values())
    ):
        raise ClangTypeUseOverlayError(
            "type-use report type_uses_deduped_total disagrees with its buckets"
        )
    if (
        isinstance(raw_total, bool)
        or not isinstance(raw_total, int)
        or raw_total < deduped_total
    ):
        raise ClangTypeUseOverlayError(
            "type-use report type_uses_raw_observations is invalid"
        )
    return buckets, counts


def _fail_closed_residuals(counts: Dict[str, int]) -> None:
    problems = [
        f"{key}={counts[key]}"
        for key in _FAIL_CLOSED_BUCKETS
        if counts[key]
    ]
    if problems:
        raise ClangTypeUseOverlayError(
            "clang type-use overlay refuses unclean type-use residuals: "
            + ", ".join(problems)
            + "; resolve or leave --clang-type-uses off"
        )


def _validate_matched_row(
    row: Dict[str, Any],
    *,
    package_dir: Path,
    digest: str,
    n_compile_entries: int,
    allowed_compilers: Set[Tuple[str, str]],
    entry_compilers: Dict[int, Tuple[str, str]],
    context: str,
) -> None:
    if row.get("classification") != "matched_internal":
        raise ClangTypeUseOverlayError(
            f"{context} has unexpected classification "
            f"{row.get('classification')!r}"
        )
    for field in (
        "use_kind",
        "owner_kind",
        "owner_name",
        "owner_tree_sitter_title",
        "owner_resolver",
        "target_entity_kind",
        "target_name",
        "target_tree_sitter_title",
        "qualType",
        "resolver",
        "source_path",
        "location_origin",
        "location_precision",
        "compile_commands_digest",
    ):
        if not isinstance(row.get(field), str) or not str(row.get(field)).strip():
            raise ClangTypeUseOverlayError(f"{context} has empty {field}")
    if row.get("location_precision") != "declaration_bearing_node":
        raise ClangTypeUseOverlayError(
            f"{context} location_precision is not declaration_bearing_node"
        )
    if str(row.get("compile_commands_digest")) != digest:
        raise ClangTypeUseOverlayError(
            f"{context} compile_commands_digest disagrees with report"
        )
    source_path = str(row["source_path"])
    source_abs = (package_dir / source_path).resolve()
    if (
        Path(source_path).is_absolute()
        or not path_is_under(source_abs, package_dir)
        or package_relative_posix(source_abs, package_dir) != source_path
    ):
        raise ClangTypeUseOverlayError(
            f"{context} has non-canonical package-relative source_path "
            f"{source_path!r}"
        )
    if row.get("owner_kind") not in _OWNER_KINDS:
        raise ClangTypeUseOverlayError(
            f"{context} has invalid owner_kind {row.get('owner_kind')!r}"
        )
    if row.get("target_entity_kind") not in _TARGET_KINDS:
        raise ClangTypeUseOverlayError(
            f"{context} has invalid target_entity_kind "
            f"{row.get('target_entity_kind')!r}"
        )
    if row.get("use_kind") not in _USE_KINDS:
        raise ClangTypeUseOverlayError(
            f"{context} has invalid use_kind {row.get('use_kind')!r}"
        )
    if row.get("resolver") not in _TARGET_RESOLVERS:
        raise ClangTypeUseOverlayError(
            f"{context} has invalid resolver {row.get('resolver')!r}"
        )
    if row.get("owner_resolver") not in _OWNER_RESOLVERS:
        raise ClangTypeUseOverlayError(
            f"{context} has invalid owner_resolver "
            f"{row.get('owner_resolver')!r}"
        )
    indices = _entry_indices(
        row.get("entry_indices"), context=f"{context}.entry_indices"
    )
    if any(i >= n_compile_entries for i in indices):
        raise ClangTypeUseOverlayError(
            f"{context}.entry_indices contains an out-of-range index"
        )
    compilers = row.get("compilers")
    if (
        not isinstance(compilers, list)
        or not compilers
        or not all(isinstance(c, dict) for c in compilers)
    ):
        raise ClangTypeUseOverlayError(
            f"{context}.compilers must be a non-empty list of objects"
        )
    row_compilers: Set[Tuple[str, str]] = set()
    for i, compiler in enumerate(compilers):
        cctx = f"{context}.compilers[{i}]"
        path = compiler.get("compiler_path")
        cid = compiler.get("compiler_id")
        cdigest = compiler.get("compile_commands_digest")
        if not isinstance(path, str) or not path.strip():
            raise ClangTypeUseOverlayError(f"{cctx} has empty compiler_path")
        if not isinstance(cid, str) or not cid.strip():
            raise ClangTypeUseOverlayError(f"{cctx} has empty compiler_id")
        if cdigest != digest:
            raise ClangTypeUseOverlayError(
                f"{cctx} compile_commands_digest disagrees with report"
            )
        identity = (path.strip(), cid.strip())
        if identity not in allowed_compilers:
            raise ClangTypeUseOverlayError(
                f"{cctx} names a compiler absent from report.compilers"
            )
        if identity in row_compilers:
            raise ClangTypeUseOverlayError(
                f"{context}.compilers contains a duplicate identity"
            )
        row_compilers.add(identity)
    expected_compilers = {entry_compilers[index] for index in indices}
    if row_compilers != expected_compilers:
        raise ClangTypeUseOverlayError(
            f"{context}.compilers disagrees with its compile entry indices"
        )
    path = row.get("compiler_path")
    cid = row.get("compiler_id")
    if len(row_compilers) == 1:
        only = next(iter(row_compilers))
        if (path, cid) != only:
            raise ClangTypeUseOverlayError(
                f"{context} singular compiler disagrees with compilers list"
            )
    elif path is not None or cid is not None:
        raise ClangTypeUseOverlayError(
            f"{context} exposes singular compiler fields for multiple compilers"
        )
    desugared = row.get("desugaredQualType")
    if desugared is not None and (
        not isinstance(desugared, str) or not desugared.strip()
    ):
        raise ClangTypeUseOverlayError(
            f"{context} has invalid desugaredQualType"
        )
    for field in ("line", "col0", "byte_offset"):
        value = row.get(field)
        if value is None:
            continue
        minimum = 1 if field == "line" else 0
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
        ):
            raise ClangTypeUseOverlayError(
                f"{context} has invalid {field} {value!r}"
            )


def _observation_record(row: Dict[str, Any]) -> Dict[str, Any]:
    """Canonical per-observation evidence payload (sorted keys later)."""
    rec: Dict[str, Any] = {
        "source_path": str(row["source_path"]),
        "location_precision": str(row["location_precision"]),
        "location_origin": str(row["location_origin"]),
        "qualType": str(row["qualType"]),
        "desugaredQualType": row.get("desugaredQualType"),
        "resolver": str(row["resolver"]),
        "owner_resolver": str(row["owner_resolver"]),
        "use_kind": str(row["use_kind"]),
        "entry_indices": sorted(int(i) for i in row["entry_indices"]),
    }
    for field in ("line", "col0", "byte_offset"):
        if row.get(field) is not None:
            rec[field] = int(row[field])
    return rec


def _aggregate_payload(
    *,
    source_entity: Dict[str, Any],
    target_entity: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
    digest: str,
    human_readable_id: int,
) -> Dict[str, Any]:
    source_title = str(source_entity["title"])
    target_title = str(target_entity["title"])
    source_id = str(source_entity["id"])
    target_id = str(target_entity["id"])

    observations = [_observation_record(r) for r in rows]
    observations.sort(key=_canonical_json)
    obs_json = _canonical_json(observations)

    use_kinds = sorted({str(r["use_kind"]) for r in rows})
    entry_indices: Set[int] = set()
    for r in rows:
        entry_indices.update(int(i) for i in r["entry_indices"])
    sorted_indices = sorted(entry_indices)

    compiler_map: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    for r in rows:
        for c in r.get("compilers") or []:
            key = (
                str(c["compiler_path"]),
                str(c["compiler_id"]),
                str(c["compile_commands_digest"]),
            )
            compiler_map[key] = {
                "compiler_path": key[0],
                "compiler_id": key[1],
                "compile_commands_digest": key[2],
            }
    compilers = [compiler_map[k] for k in sorted(compiler_map)]
    compilers_json = _canonical_json(compilers)
    if len(compilers) == 1:
        compiler_path = compilers[0]["compiler_path"]
        compiler_id = compilers[0]["compiler_id"]
    else:
        compiler_path = None
        compiler_id = None

    # Prefer a stable source_file from the earliest observation path.
    source_files = sorted(
        {str(r.get("source_path") or "") for r in rows if r.get("source_path")}
    )
    source_file = source_files[0] if source_files else ""

    desc = (
        f"configured Clang type-use evidence: {source_title} uses_type "
        f"{target_title} ({len(rows)} observation(s); kinds={use_kinds}; "
        f"fact_kind={FACT_KIND}; deterministic only relative to recorded "
        f"Clang + compile_commands.json)"
    )
    return {
        "id": _relationship_id(
            source_title,
            target_title,
            source_id,
            target_id,
        ),
        "source": source_title,
        "target": target_title,
        "type": REL_TYPE,
        "description": desc,
        "weight": 1.0,
        "text_unit_ids": [],
        "human_readable_id": human_readable_id,
        "source_file": source_file,
        "span": "",
        "extractor": EXTRACTOR,
        "confidence": 1.0,
        "is_deterministic": True,
        "document_ids": list(source_entity.get("document_ids") or [])
        or list(target_entity.get("document_ids") or [])
        or [],
        "covariate_ids": [],
        "fact_kind": FACT_KIND,
        "clang_type_use_status": "matched",
        "clang_type_use_fact_kind": FACT_KIND,
        "clang_type_use_extractor": EXTRACTOR,
        "clang_type_use_confidence": 1.0,
        "clang_type_use_is_deterministic": True,
        "clang_type_use_observation_count": len(rows),
        "clang_type_use_use_kinds": use_kinds,
        "clang_type_use_entry_indices": sorted_indices,
        "clang_type_use_compiler_path": compiler_path,
        "clang_type_use_compiler_id": compiler_id,
        "clang_type_use_compilers": compilers_json,
        "clang_type_use_compile_commands_digest": digest,
        "clang_type_use_observations_json": obs_json,
        "clang_type_use_source_entity_id": source_id,
        "clang_type_use_target_entity_id": target_id,
        "clang_type_use_description": desc,
    }


def apply_clang_type_uses_from_report(
    data: Dict[str, List[Dict[str, Any]]],
    report: Dict[str, Any],
    package_dir: Path,
) -> Dict[str, Any]:
    """Apply a precomputed type-use report as aggregated ``uses_type`` edges.

    Plans fully before mutating. On error, ``data`` is left unchanged.
    """
    package_dir = Path(package_dir).resolve()
    if str(report.get("mode") or "") != "clang_ast_json_type_use_audit":
        raise ClangTypeUseOverlayError(
            f"unexpected type-use audit mode {report.get('mode')!r}; expected "
            "clang_ast_json_type_use_audit"
        )
    if str(report.get("package") or "") != package_dir.name:
        raise ClangTypeUseOverlayError(
            f"type-use report package {report.get('package')!r} does not match "
            f"target package {package_dir.name!r}"
        )
    raw_digest = report.get("compile_commands_digest")
    if not isinstance(raw_digest, str) or not raw_digest.strip():
        raise ClangTypeUseOverlayError(
            "type-use report has empty compile_commands_digest"
        )
    digest = raw_digest.strip()
    n_compile_entries = report.get("n_compile_entries")
    translation_units = report.get("translation_units")
    if (
        isinstance(n_compile_entries, bool)
        or not isinstance(n_compile_entries, int)
        or n_compile_entries <= 0
    ):
        raise ClangTypeUseOverlayError(
            "type-use report n_compile_entries must be a positive integer"
        )
    if (
        not isinstance(translation_units, list)
        or not all(isinstance(t, dict) for t in translation_units)
        or len(translation_units) != n_compile_entries
    ):
        raise ClangTypeUseOverlayError(
            "type-use report translation_units must contain exactly one row "
            "per compile entry"
        )

    compilers = report.get("compilers")
    if (
        not isinstance(compilers, list)
        or not compilers
        or not all(isinstance(c, dict) for c in compilers)
    ):
        raise ClangTypeUseOverlayError(
            "type-use report compilers must be a list of provenance objects"
        )
    allowed_compilers: Set[Tuple[str, str]] = set()
    normalized_compilers: List[Dict[str, Any]] = []
    for position, compiler in enumerate(compilers):
        path = compiler.get("compiler_path")
        cid = compiler.get("compiler_id")
        if (
            not isinstance(path, str)
            or not path.strip()
            or not isinstance(cid, str)
            or not cid.strip()
        ):
            raise ClangTypeUseOverlayError(
                f"type-use report compiler {position} has incomplete identity"
            )
        identity = (path.strip(), cid.strip())
        if identity in allowed_compilers:
            raise ClangTypeUseOverlayError(
                f"duplicate compiler identity in report.compilers: {identity!r}"
            )
        if not Path(identity[0]).is_absolute():
            raise ClangTypeUseOverlayError(
                f"type-use report compiler {position} path is not absolute: "
                f"{identity[0]!r}"
            )
        allowed_compilers.add(identity)
        normalized = dict(compiler)
        normalized["compiler_path"] = identity[0]
        normalized["compiler_id"] = identity[1]
        normalized_compilers.append(normalized)
    try:
        normalized_compilers.sort(key=_canonical_json)
    except (TypeError, ValueError) as e:
        raise ClangTypeUseOverlayError(
            "type-use report compiler provenance is not JSON-serializable"
        ) from e

    entry_compilers: Dict[int, Tuple[str, str]] = {}
    for position, translation_unit in enumerate(translation_units):
        entry_index = translation_unit.get("entry_index")
        if (
            isinstance(entry_index, bool)
            or not isinstance(entry_index, int)
            or entry_index < 0
            or entry_index >= n_compile_entries
            or entry_index in entry_compilers
        ):
            raise ClangTypeUseOverlayError(
                f"type-use translation unit {position} has invalid or "
                f"duplicate entry_index {entry_index!r}"
            )
        tu_path = translation_unit.get("compiler_path")
        tu_id = translation_unit.get("compiler_id")
        if not isinstance(tu_path, str) or not isinstance(tu_id, str):
            raise ClangTypeUseOverlayError(
                f"type-use translation unit {position} has incomplete "
                "compiler identity"
            )
        identity = (tu_path.strip(), tu_id.strip())
        if identity not in allowed_compilers:
            raise ClangTypeUseOverlayError(
                f"type-use translation unit {position} names a compiler "
                "absent from report.compilers"
            )
        package_local = translation_unit.get("package_local")
        tu_file = translation_unit.get("file")
        if not isinstance(package_local, bool):
            raise ClangTypeUseOverlayError(
                f"type-use translation unit {position} has invalid "
                "package_local flag"
            )
        if package_local:
            if not isinstance(tu_file, str) or not tu_file:
                raise ClangTypeUseOverlayError(
                    f"type-use translation unit {position} lacks its "
                    "package-local file"
                )
            tu_abs = (package_dir / tu_file).resolve()
            if (
                Path(tu_file).is_absolute()
                or not path_is_under(tu_abs, package_dir)
                or package_relative_posix(tu_abs, package_dir) != tu_file
            ):
                raise ClangTypeUseOverlayError(
                    f"type-use translation unit {position} has non-canonical "
                    f"package-local file {tu_file!r}"
                )
        elif tu_file is not None:
            raise ClangTypeUseOverlayError(
                f"type-use translation unit {position} is outside-package "
                "but exposes a file"
            )
        entry_compilers[entry_index] = identity
    if set(entry_compilers) != set(range(n_compile_entries)):
        raise ClangTypeUseOverlayError(
            "type-use translation-unit entry indices are not a complete census"
        )

    top_path = report.get("compiler_path")
    top_id = report.get("compiler_id")
    top_version = report.get("compiler_version")
    if len(allowed_compilers) == 1:
        only_path, only_id = next(iter(allowed_compilers))
        if (top_path, top_id) != (only_path, only_id):
            raise ClangTypeUseOverlayError(
                "singular compiler identity disagrees with report.compilers"
            )
        only_version = normalized_compilers[0].get("compiler_version")
        if top_version is not None and top_version != only_version:
            raise ClangTypeUseOverlayError(
                "singular compiler_version disagrees with report.compilers"
            )
    elif any(v is not None for v in (top_path, top_id, top_version)):
        raise ClangTypeUseOverlayError(
            "multi-compiler type-use report must not expose a singular "
            "compiler identity"
        )

    buckets, counts = _validated_buckets(report)
    _fail_closed_residuals(counts)

    matched = buckets["matched_internal"]
    seen_matched_rows: Set[str] = set()
    for i, row in enumerate(matched):
        _validate_matched_row(
            row,
            package_dir=package_dir,
            digest=digest,
            n_compile_entries=n_compile_entries,
            allowed_compilers=allowed_compilers,
            entry_compilers=entry_compilers,
            context=f"matched_internal[{i}]",
        )
        try:
            row_key = _canonical_json(row)
        except (TypeError, ValueError) as e:
            raise ClangTypeUseOverlayError(
                f"matched_internal[{i}] is not JSON-serializable"
            ) from e
        if row_key in seen_matched_rows:
            raise ClangTypeUseOverlayError(
                f"duplicate matched_internal observation at row {i}"
            )
        seen_matched_rows.add(row_key)

    by_title, entities_by_id = _index_entities(data)
    # Group by entity-id pair.
    groups: Dict[
        Tuple[str, str],
        Dict[str, Any],
    ] = {}
    # value: source_entity, target_entity, rows

    for i, row in enumerate(matched):
        ctx = f"matched_internal[{i}]"
        owner_title = str(row["owner_tree_sitter_title"])
        target_title = str(row["target_tree_sitter_title"])
        owner_kind = str(row["owner_kind"])
        target_kind = str(row["target_entity_kind"])

        owners = by_title.get(owner_title) or []
        if len(owners) == 0:
            raise ClangTypeUseOverlayError(
                f"{ctx}: no entity for owner title {owner_title!r}"
            )
        if len(owners) > 1:
            raise ClangTypeUseOverlayError(
                f"{ctx}: non-unique owner title {owner_title!r} "
                f"({len(owners)} entities)"
            )
        targets = by_title.get(target_title) or []
        if len(targets) == 0:
            raise ClangTypeUseOverlayError(
                f"{ctx}: no entity for target title {target_title!r}"
            )
        if len(targets) > 1:
            raise ClangTypeUseOverlayError(
                f"{ctx}: non-unique target title {target_title!r} "
                f"({len(targets)} entities)"
            )
        source_entity = owners[0]
        target_entity = targets[0]
        if str(source_entity.get("type") or "") != owner_kind:
            raise ClangTypeUseOverlayError(
                f"{ctx}: owner type mismatch for {owner_title!r}: "
                f"graph={source_entity.get('type')!r} audit={owner_kind!r}"
            )
        if str(target_entity.get("type") or "") != target_kind:
            raise ClangTypeUseOverlayError(
                f"{ctx}: target type mismatch for {target_title!r}: "
                f"graph={target_entity.get('type')!r} audit={target_kind!r}"
            )
        if source_entity.get("symbol_name") != row.get("owner_name"):
            raise ClangTypeUseOverlayError(
                f"{ctx}: owner symbol_name mismatch for {owner_title!r}: "
                f"graph={source_entity.get('symbol_name')!r} "
                f"audit={row.get('owner_name')!r}"
            )
        if target_entity.get("symbol_name") != row.get("target_name"):
            raise ClangTypeUseOverlayError(
                f"{ctx}: target symbol_name mismatch for {target_title!r}: "
                f"graph={target_entity.get('symbol_name')!r} "
                f"audit={row.get('target_name')!r}"
            )
        src_id = str(source_entity.get("id") or "")
        tgt_id = str(target_entity.get("id") or "")
        if not src_id or not tgt_id:
            raise ClangTypeUseOverlayError(
                f"{ctx}: missing entity id for {owner_title!r} / {target_title!r}"
            )
        key = (src_id, tgt_id)
        if key not in groups:
            groups[key] = {
                "source_entity": source_entity,
                "target_entity": target_entity,
                "rows": [],
            }
        else:
            # Same entity ids must always resolve to the same titles.
            if groups[key]["source_entity"] is not source_entity:
                if groups[key]["source_entity"].get("title") != owner_title:
                    raise ClangTypeUseOverlayError(
                        f"{ctx}: entity-id collision for source {src_id!r}"
                    )
            if groups[key]["target_entity"] is not target_entity:
                if groups[key]["target_entity"].get("title") != target_title:
                    raise ClangTypeUseOverlayError(
                        f"{ctx}: entity-id collision for target {tgt_id!r}"
                    )
        groups[key]["rows"].append(row)

    relationships = data.get("relationships")
    if not isinstance(relationships, list) or not all(
        isinstance(r, dict) for r in relationships
    ):
        raise ClangTypeUseOverlayError(
            "data.relationships must be a list of objects"
        )

    # Snapshot pre-existing relationship identity for "unchanged IDs" checks.
    baseline_ids = [
        str(r.get("id"))
        for r in relationships
        if str(r.get("type")) != REL_TYPE
        or str(r.get("fact_kind")) != FACT_KIND
    ]

    # Index all relationship IDs plus existing uses_type edges from this overlay.
    existing_by_pair: Dict[Tuple[str, str], Dict[str, Any]] = {}
    existing_by_id: Dict[str, Dict[str, Any]] = {}
    for rel in relationships:
        rid = str(rel.get("id") or "")
        if not rid:
            raise ClangTypeUseOverlayError(
                "every graph relationship must have a non-empty id"
            )
        if rid in existing_by_id:
            raise ClangTypeUseOverlayError(
                f"duplicate relationship id {rid!r}"
            )
        existing_by_id[rid] = rel
        if str(rel.get("type")) != REL_TYPE:
            # Stale clang_type_use_* on non-uses_type relationships.
            stale = sorted(
                str(k)
                for k, v in rel.items()
                if str(k).startswith("clang_type_use_")
                and v is not None
                and not (isinstance(v, float) and math.isnan(v))
            )
            if stale:
                raise ClangTypeUseOverlayError(
                    f"stale clang_type_use_* fields on non-uses_type "
                    f"relationship {rel.get('id')!r}: {stale}"
                )
            continue
        if str(rel.get("fact_kind") or "") != FACT_KIND:
            raise ClangTypeUseOverlayError(
                f"conflicting uses_type relationship {rel.get('id')!r} has "
                f"fact_kind={rel.get('fact_kind')!r}"
            )
        # A configured edge must carry collision-safe entity-id endpoints.
        src_eid = rel.get("clang_type_use_source_entity_id")
        tgt_eid = rel.get("clang_type_use_target_entity_id")
        if not src_eid or not tgt_eid:
            raise ClangTypeUseOverlayError(
                f"configured uses_type relationship {rid!r} lacks entity-id "
                "endpoints"
            )
        pair = (str(src_eid), str(tgt_eid))
        if pair in existing_by_pair:
            raise ClangTypeUseOverlayError(
                f"duplicate pre-existing uses_type pair {pair!r}"
            )
        source_entity = entities_by_id.get(pair[0])
        target_entity = entities_by_id.get(pair[1])
        if (
            source_entity is None
            or target_entity is None
            or rel.get("source") != source_entity.get("title")
            or rel.get("target") != target_entity.get("title")
        ):
            raise ClangTypeUseOverlayError(
                f"configured uses_type relationship {rid!r} has inconsistent "
                "entity-id/title endpoints"
            )
        existing_by_pair[pair] = rel

    planned: List[Tuple[Optional[Dict[str, Any]], Dict[str, Any]]] = []
    planned_pairs: Set[Tuple[str, str]] = set()
    planned_ids: Set[str] = set()

    # Deterministic order by source/target titles.
    ordered_keys = sorted(
        groups.keys(),
        key=lambda k: (
            str(groups[k]["source_entity"].get("title") or ""),
            str(groups[k]["target_entity"].get("title") or ""),
            k[0],
            k[1],
        ),
    )
    next_hid = next_human_readable_id(relationships)
    for key in ordered_keys:
        group = groups[key]
        existing = existing_by_pair.get(key)
        if existing is not None:
            existing_hid = existing.get("human_readable_id")
            if (
                isinstance(existing_hid, bool)
                or not isinstance(existing_hid, int)
                or existing_hid < 0
            ):
                raise ClangTypeUseOverlayError(
                    f"configured uses_type relationship {existing.get('id')!r} "
                    "has invalid human_readable_id"
                )
            hid = existing_hid
        else:
            hid = next_hid
        if existing is None:
            next_hid = max(next_hid, hid + 1)
        # Stable observation order for aggregation.
        rows_sorted = sorted(
            group["rows"],
            key=lambda r: (
                str(r.get("source_path") or ""),
                int(r.get("line") or 0),
                int(r.get("col0") or 0),
                str(r.get("use_kind") or ""),
                str(r.get("qualType") or ""),
                _canonical_json(r),
            ),
        )
        payload = _aggregate_payload(
            source_entity=group["source_entity"],
            target_entity=group["target_entity"],
            rows=rows_sorted,
            digest=digest,
            human_readable_id=hid,
        )
        rid = payload["id"]
        if rid in planned_ids:
            raise ClangTypeUseOverlayError(
                f"planned relationship id collision {rid!r}"
            )
        planned_ids.add(rid)
        planned_pairs.add(key)

        if existing is not None:
            # Same endpoints must not claim a different relationship id.
            if str(existing.get("id") or "") != rid:
                raise ClangTypeUseOverlayError(
                    f"conflicting pre-existing relationship id for "
                    f"{payload['source']}->{payload['target']}: "
                    f"existing={existing.get('id')!r} new={rid!r}"
                )
            unknown = sorted(
                str(k)
                for k, v in existing.items()
                if str(k).startswith("clang_type_use_")
                and str(k) not in _TYPE_USE_FIELDS
                and v is not None
                and not (isinstance(v, float) and math.isnan(v))
            )
            if unknown:
                raise ClangTypeUseOverlayError(
                    f"unknown pre-existing clang_type_use_* fields on "
                    f"{rid!r}: {unknown}"
                )
            for k, new_val in payload.items():
                if k == "human_readable_id":
                    # Preserve existing human_readable_id when present.
                    continue
                if k in existing and not _values_compatible(
                    existing.get(k), new_val
                ):
                    raise ClangTypeUseOverlayError(
                        f"conflicting pre-existing field {k!r} on {rid!r}: "
                        f"existing={existing.get(k)!r} new={new_val!r}"
                    )
            # Keep the existing human_readable_id in the applied payload.
            payload["human_readable_id"] = existing.get(
                "human_readable_id", payload["human_readable_id"]
            )
            planned.append((existing, payload))
        else:
            # ID must not collide with any unrelated existing relationship.
            if rid in existing_by_id:
                raise ClangTypeUseOverlayError(
                    f"relationship id {rid!r} already exists with different "
                    "endpoints/metadata"
                )
            planned.append((None, payload))

    # Stale overlay edges not selected by this report.
    for pair, rel in existing_by_pair.items():
        if pair not in planned_pairs:
            raise ClangTypeUseOverlayError(
                f"stale uses_type relationship not in current report: "
                f"{rel.get('id')!r} pair={pair!r}"
            )
    for rel in relationships:
        if str(rel.get("type")) != REL_TYPE:
            continue
        rid = str(rel.get("id") or "")
        if rid and rid not in planned_ids:
            raise ClangTypeUseOverlayError(
                f"stale configured uses_type relationship {rid!r}"
            )

    # Mutation begins only after full validation.
    n_changed = 0
    n_added = 0
    for existing, payload in planned:
        if existing is None:
            relationships.append(dict(payload))
            n_added += 1
            n_changed += 1
            continue
        changed = False
        for k, new_val in payload.items():
            if k not in existing or existing.get(k) != new_val:
                existing[k] = new_val
                changed = True
        if changed:
            n_changed += 1

    # Pre-existing non-overlay relationship ids must remain.
    after_non_overlay = [
        str(r.get("id"))
        for r in relationships
        if str(r.get("type")) != REL_TYPE
        or str(r.get("fact_kind")) != FACT_KIND
    ]
    if after_non_overlay != baseline_ids:
        raise ClangTypeUseOverlayError(
            "internal error: overlay mutated pre-existing non-uses_type "
            "relationship identities"
        )

    return {
        "mode": MODE,
        "enabled": True,
        "fact_kind": FACT_KIND,
        "extractor": EXTRACTOR,
        "n_facts": len(planned),
        "n_facts_changed": n_changed,
        "n_facts_added": n_added,
        "n_observations": len(matched),
        "n_compile_entries": n_compile_entries,
        "n_translation_units": len(translation_units),
        "compiler_path": report.get("compiler_path"),
        "compiler_id": report.get("compiler_id"),
        "compiler_version": report.get("compiler_version"),
        "compilers": normalized_compilers,
        "compile_commands_digest": digest,
        "counts": {key: counts[key] for key in _ALL_BUCKETS},
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }


def append_clang_type_uses(
    data: Dict[str, List[Dict[str, Any]]],
    package_dir: Path,
    *,
    timeout: int = 120,
    report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the type-use audit (unless ``report`` given) and attach uses_type edges."""
    package_dir = Path(package_dir).resolve()
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout <= 0
    ):
        raise ClangTypeUseOverlayError(
            f"timeout must be a positive integer, got {timeout!r}"
        )
    if report is None:
        try:
            report = run_clang_type_use_audit(package_dir, timeout=timeout)
        except ClangTypeUseAuditError as e:
            raise ClangTypeUseOverlayError(str(e)) from e
    return apply_clang_type_uses_from_report(data, report, package_dir)


# ---------------------------------------------------------------------------
# Read-only integrity audit for *already persisted* configured uses_type edges.
# Does not invoke Clang, reindex, publish, or mutate inputs.
# ---------------------------------------------------------------------------

_MAX_ANOMALY_SAMPLES = 40
_MAX_ANOMALY_MESSAGE = 240


def _clip_sample(text: str, limit: int = _MAX_ANOMALY_MESSAGE) -> str:
    s = str(text)
    if len(s) <= limit:
        return s
    return s[: limit - 3] + "..."


# Typing helper: plain dicts, pandas Series, and other mapping-like rows.
Mappingish = Any


def _as_row_mapping(row: Any) -> Dict[str, Any]:
    """Read-only view of a relationship/entity row without mutating the source."""
    if isinstance(row, dict):
        return row
    if hasattr(row, "items"):
        return dict(row)
    raise TypeError(f"expected mapping row, got {type(row)!r}")


def _row_get(row: Mappingish, key: str, default: Any = None) -> Any:
    try:
        return row.get(key, default)  # type: ignore[attr-defined]
    except Exception:
        try:
            return row[key]  # type: ignore[index]
        except Exception:
            return default


def _normalize_list_field(value: Any) -> Optional[List[Any]]:
    """Best-effort list extraction for parquet ndarray/list cells.

    Returns None when the cell is absent/null/NaN. Does **not** invent
    content for malformed scalars — callers treat non-list material values
    as anomalies.
    """
    if not is_material_value(value):
        return None
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        try:
            converted = value.tolist()
        except Exception:
            return None
        if isinstance(converted, list):
            return converted
        return None
    return None


def _table_rows(table: Any, *, name: str) -> List[Dict[str, Any]]:
    """Accept a dataframe or a sequence of mapping rows without mutation."""
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
    return [_as_row_mapping(row) for row in records]


def _truthy_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    return None


def _float_eq_one(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return False
    return float(value) == 1.0


def _is_configured_uses_type(rel: Mappingish) -> bool:
    return (
        str(_row_get(rel, "type") or "") == REL_TYPE
        and str(_row_get(rel, "fact_kind") or "") == FACT_KIND
    )


def _has_material_type_use_fields(rel: Mappingish) -> bool:
    for key in list(rel.keys()) if hasattr(rel, "keys") else []:
        k = str(key)
        if not k.startswith("clang_type_use_"):
            continue
        if is_material_value(_row_get(rel, k)):
            return True
    return False


def _anomaly(
    code: str,
    message: str,
    *,
    relationship_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if code not in ANOMALY_CODES:
        raise AssertionError(f"unknown type-use integrity anomaly code {code!r}")
    row: Dict[str, Any] = {
        "code": code,
        "message": _clip_sample(message),
    }
    if relationship_id is not None:
        row["relationship_id"] = relationship_id
    if extra:
        for k, v in sorted(extra.items()):
            if isinstance(v, str):
                row[k] = _clip_sample(v)
            else:
                row[k] = v
    return row


def _decode_observations_strict(
    raw: Any,
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Strict decode of clang_type_use_observations_json.

    Returns (observations, error_code). Never invents observations.
    """
    if not is_material_value(raw):
        return None, "malformed_observations_json"
    if _contains_non_finite(raw):
        return None, "nan_or_infinity"
    if isinstance(raw, (list, tuple)):
        # Unexpected non-string storage: still validate structure if list of dicts.
        items = list(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, "empty_observations"
        try:
            parsed = _strict_json_loads(text)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            if "non-finite JSON constant" in str(error):
                return None, "nan_or_infinity"
            return None, "malformed_observations_json"
        if not isinstance(parsed, list):
            return None, "malformed_observations_json"
        items = parsed
    else:
        # ndarray of objects, etc.
        normalized = _normalize_list_field(raw)
        if normalized is None:
            return None, "malformed_observations_json"
        items = normalized
    if not items:
        return None, "empty_observations"
    out: List[Dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            return None, "non_object_observation"
        if _contains_non_finite(item):
            return None, "nan_or_infinity"
        out.append(item)
    return out, None


_OBSERVATION_REQUIRED_FIELDS = frozenset(
    {
        "source_path",
        "location_precision",
        "location_origin",
        "qualType",
        "desugaredQualType",
        "resolver",
        "owner_resolver",
        "use_kind",
        "entry_indices",
    }
)
_OBSERVATION_OPTIONAL_FIELDS = frozenset({"line", "col0", "byte_offset"})


def _observation_schema_error(observation: Dict[str, Any]) -> Optional[str]:
    """Return a persisted producer-schema error without rewriting evidence."""
    keys = set(observation)
    missing = sorted(_OBSERVATION_REQUIRED_FIELDS - keys)
    unknown = sorted(
        keys - _OBSERVATION_REQUIRED_FIELDS - _OBSERVATION_OPTIONAL_FIELDS
    )
    if missing or unknown:
        return f"missing fields={missing!r}; unknown fields={unknown!r}"

    for field in (
        "source_path",
        "location_origin",
        "qualType",
        "resolver",
        "owner_resolver",
        "use_kind",
    ):
        value = observation.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"{field} must be a non-empty string"
    source_path = str(observation["source_path"])
    if (
        Path(source_path).is_absolute()
        or "\\" in source_path
        or ".." in Path(source_path).parts
    ):
        return "source_path must be canonical package-relative POSIX"
    if observation.get("location_precision") != "declaration_bearing_node":
        return "location_precision must be declaration_bearing_node"
    if observation.get("use_kind") not in _USE_KINDS:
        return f"invalid use_kind {observation.get('use_kind')!r}"
    if observation.get("resolver") not in _TARGET_RESOLVERS:
        return f"invalid resolver {observation.get('resolver')!r}"
    if observation.get("owner_resolver") not in _OWNER_RESOLVERS:
        return f"invalid owner_resolver {observation.get('owner_resolver')!r}"
    desugared = observation.get("desugaredQualType")
    if desugared is not None and (
        not isinstance(desugared, str) or not desugared.strip()
    ):
        return "desugaredQualType must be null or a non-empty string"

    indices = observation.get("entry_indices")
    if (
        not isinstance(indices, list)
        or not indices
        or any(
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            for index in indices
        )
        or indices != sorted(set(indices))
    ):
        return "entry_indices must be a non-empty sorted unique integer list"

    for field in _OBSERVATION_OPTIONAL_FIELDS:
        if field not in observation:
            continue
        value = observation[field]
        minimum = 1 if field == "line" else 0
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < minimum
        ):
            return f"{field} must be an integer >= {minimum}"
    return None


def _validate_one_configured_edge(
    rel: Mappingish,
    *,
    by_title: Dict[str, List[Dict[str, Any]]],
    by_id: Dict[str, Dict[str, Any]],
    pair_index: Dict[Tuple[str, str], str],
) -> Tuple[List[Dict[str, Any]], int]:
    """Validate one configured uses_type relationship. Returns (anomalies, obs_count)."""
    anomalies: List[Dict[str, Any]] = []
    rid = str(_row_get(rel, "id") or "")
    obs_count_out = 0

    # Mirrored fact/extractor fields.
    if str(_row_get(rel, "type") or "") != REL_TYPE:
        anomalies.append(
            _anomaly(
                "field_mismatch",
                f"type is {_row_get(rel, 'type')!r}, expected {REL_TYPE!r}",
                relationship_id=rid or None,
            )
        )
    fact = _row_get(rel, "fact_kind")
    mirrored_fact = _row_get(rel, "clang_type_use_fact_kind")
    if str(fact or "") != FACT_KIND or str(mirrored_fact or "") != FACT_KIND:
        anomalies.append(
            _anomaly(
                "field_mismatch",
                f"fact_kind/mirrored fact disagree or wrong: "
                f"fact_kind={fact!r} clang_type_use_fact_kind={mirrored_fact!r}",
                relationship_id=rid or None,
            )
        )
    extractor = _row_get(rel, "extractor")
    mirrored_ext = _row_get(rel, "clang_type_use_extractor")
    if str(extractor or "") != EXTRACTOR or str(mirrored_ext or "") != EXTRACTOR:
        anomalies.append(
            _anomaly(
                "field_mismatch",
                f"extractor/mirrored extractor disagree or wrong: "
                f"extractor={extractor!r} clang_type_use_extractor={mirrored_ext!r}",
                relationship_id=rid or None,
            )
        )
    status = _row_get(rel, "clang_type_use_status")
    if is_material_value(status) and str(status) != "matched":
        anomalies.append(
            _anomaly(
                "field_mismatch",
                f"clang_type_use_status={status!r}, expected 'matched'",
                relationship_id=rid or None,
            )
        )

    # Confidence boundary (toolchain/configuration-relative).
    conf = _row_get(rel, "confidence")
    conf_m = _row_get(rel, "clang_type_use_confidence")
    det = _row_get(rel, "is_deterministic")
    det_m = _row_get(rel, "clang_type_use_is_deterministic")
    for label, value in (
        ("confidence", conf),
        ("clang_type_use_confidence", conf_m),
    ):
        if isinstance(value, float) and (
            math.isnan(value) or math.isinf(value)
        ):
            anomalies.append(
                _anomaly(
                    "nan_or_infinity",
                    f"{label} is NaN/Infinity",
                    relationship_id=rid or None,
                )
            )
        elif not _float_eq_one(value):
            anomalies.append(
                _anomaly(
                    "confidence_boundary",
                    f"{label}={value!r}, expected 1.0 "
                    f"(configuration/toolchain-relative only)",
                    relationship_id=rid or None,
                )
            )
    for label, value in (
        ("is_deterministic", det),
        ("clang_type_use_is_deterministic", det_m),
    ):
        if _truthy_bool(value) is not True:
            # bool True only; reject 1, "true", None, False.
            if isinstance(value, float) and (
                math.isnan(value) or math.isinf(value)
            ):
                anomalies.append(
                    _anomaly(
                        "nan_or_infinity",
                        f"{label} is NaN/Infinity",
                        relationship_id=rid or None,
                    )
                )
            else:
                anomalies.append(
                    _anomaly(
                        "confidence_boundary",
                        f"{label}={value!r}, expected True "
                        f"(configuration/toolchain-relative only)",
                        relationship_id=rid or None,
                    )
                )

    # Unknown material clang_type_use_* fields.
    known = set(TYPE_USE_FIELDS)
    for key in sorted(str(k) for k in (rel.keys() if hasattr(rel, "keys") else [])):
        if not key.startswith("clang_type_use_"):
            continue
        if key in known:
            continue
        if is_material_value(_row_get(rel, key)):
            anomalies.append(
                _anomaly(
                    "unknown_type_use_field",
                    f"unknown material field {key!r}",
                    relationship_id=rid or None,
                    extra={"field": key},
                )
            )

    # Human readable id.
    hid = _row_get(rel, "human_readable_id")
    if (
        isinstance(hid, bool)
        or not isinstance(hid, int)
        or hid < 0
        or (isinstance(hid, float) and (math.isnan(hid) or math.isinf(hid)))
    ):
        # numpy int64 is subclass of int in some builds; also accept numpy integers.
        ok_hid = False
        if not isinstance(hid, bool) and isinstance(hid, (int, float)):
            if isinstance(hid, float) and (math.isnan(hid) or math.isinf(hid)):
                ok_hid = False
            elif float(hid) == int(hid) and int(hid) >= 0:
                ok_hid = True
        if not ok_hid:
            anomalies.append(
                _anomaly(
                    "invalid_human_readable_id",
                    f"human_readable_id={hid!r} is not a non-negative integer",
                    relationship_id=rid or None,
                )
            )

    source_title = str(_row_get(rel, "source") or "")
    target_title = str(_row_get(rel, "target") or "")
    src_eid_field = _row_get(rel, "clang_type_use_source_entity_id")
    tgt_eid_field = _row_get(rel, "clang_type_use_target_entity_id")
    src_eid = str(src_eid_field) if is_material_value(src_eid_field) else ""
    tgt_eid = str(tgt_eid_field) if is_material_value(tgt_eid_field) else ""

    # Title resolution (exactly one entity per endpoint title).
    source_ents = by_title.get(source_title) or []
    target_ents = by_title.get(target_title) or []
    source_entity: Optional[Dict[str, Any]] = None
    target_entity: Optional[Dict[str, Any]] = None
    if not source_title or len(source_ents) == 0:
        anomalies.append(
            _anomaly(
                "dangling_source",
                f"source title {source_title!r} resolves to no entity",
                relationship_id=rid or None,
            )
        )
    elif len(source_ents) > 1:
        anomalies.append(
            _anomaly(
                "ambiguous_source_title",
                f"source title {source_title!r} resolves to "
                f"{len(source_ents)} entities",
                relationship_id=rid or None,
            )
        )
    else:
        source_entity = source_ents[0]
    if not target_title or len(target_ents) == 0:
        anomalies.append(
            _anomaly(
                "dangling_target",
                f"target title {target_title!r} resolves to no entity",
                relationship_id=rid or None,
            )
        )
    elif len(target_ents) > 1:
        anomalies.append(
            _anomaly(
                "ambiguous_target_title",
                f"target title {target_title!r} resolves to "
                f"{len(target_ents)} entities",
                relationship_id=rid or None,
            )
        )
    else:
        target_entity = target_ents[0]

    if source_entity is not None:
        real_src_id = str(source_entity.get("id") or "")
        if not src_eid or src_eid != real_src_id:
            anomalies.append(
                _anomaly(
                    "entity_id_mismatch",
                    f"clang_type_use_source_entity_id={src_eid!r} "
                    f"!= entity.id={real_src_id!r}",
                    relationship_id=rid or None,
                )
            )
        src_kind = str(source_entity.get("type") or "")
        if src_kind not in _OWNER_KINDS:
            anomalies.append(
                _anomaly(
                    "invalid_source_kind",
                    f"source entity kind {src_kind!r} not in producer contract "
                    f"{sorted(_OWNER_KINDS)}",
                    relationship_id=rid or None,
                )
            )
        # Entity-id index consistency when stored id is present.
        if src_eid and src_eid in by_id and by_id[src_eid] is not source_entity:
            if str(by_id[src_eid].get("title") or "") != source_title:
                anomalies.append(
                    _anomaly(
                        "entity_id_mismatch",
                        f"source entity id {src_eid!r} maps to a different title",
                        relationship_id=rid or None,
                    )
                )
    if target_entity is not None:
        real_tgt_id = str(target_entity.get("id") or "")
        if not tgt_eid or tgt_eid != real_tgt_id:
            anomalies.append(
                _anomaly(
                    "entity_id_mismatch",
                    f"clang_type_use_target_entity_id={tgt_eid!r} "
                    f"!= entity.id={real_tgt_id!r}",
                    relationship_id=rid or None,
                )
            )
        tgt_kind = str(target_entity.get("type") or "")
        if tgt_kind not in _TARGET_KINDS:
            anomalies.append(
                _anomaly(
                    "invalid_target_kind",
                    f"target entity kind {tgt_kind!r} not in producer contract "
                    f"{sorted(_TARGET_KINDS)}",
                    relationship_id=rid or None,
                )
            )

    # Deterministic relationship id (requires resolved endpoints).
    if (
        source_entity is not None
        and target_entity is not None
        and src_eid
        and tgt_eid
        and source_title
        and target_title
    ):
        expected_id = relationship_id(
            source_title,
            target_title,
            str(source_entity.get("id") or src_eid),
            str(target_entity.get("id") or tgt_eid),
        )
        if rid != expected_id:
            anomalies.append(
                _anomaly(
                    "relationship_id_mismatch",
                    f"stored id {rid!r} != producer id {expected_id!r}",
                    relationship_id=rid or None,
                    extra={"expected_id": expected_id},
                )
            )
        pair = (
            str(source_entity.get("id") or src_eid),
            str(target_entity.get("id") or tgt_eid),
        )
        prior = pair_index.get(pair)
        if prior is not None and prior != rid:
            anomalies.append(
                _anomaly(
                    "duplicate_endpoint_pair",
                    f"duplicate configured edge for entity-id pair {pair!r}; "
                    f"also {prior!r}",
                    relationship_id=rid or None,
                    extra={"other_relationship_id": prior},
                )
            )
        else:
            pair_index[pair] = rid

    # --- Aggregated evidence ---
    observations, obs_err = _decode_observations_strict(
        _row_get(rel, "clang_type_use_observations_json")
    )
    declared_count = _row_get(rel, "clang_type_use_observation_count")
    if obs_err == "non_object_observation":
        anomalies.append(
            _anomaly(
                "non_object_observation",
                "observations_json contains a non-object item",
                relationship_id=rid or None,
            )
        )
    elif obs_err == "empty_observations":
        anomalies.append(
            _anomaly(
                "empty_observations",
                "observations_json is empty",
                relationship_id=rid or None,
            )
        )
    elif obs_err == "nan_or_infinity":
        anomalies.append(
            _anomaly(
                "nan_or_infinity",
                "observations_json is NaN/Infinity",
                relationship_id=rid or None,
            )
        )
    elif obs_err is not None:
        anomalies.append(
            _anomaly(
                "malformed_observations_json",
                "observations_json is missing or not strict JSON list of objects",
                relationship_id=rid or None,
            )
        )
    else:
        assert observations is not None
        obs_count_out = len(observations)
        for index, observation in enumerate(observations):
            schema_error = _observation_schema_error(observation)
            if schema_error is not None:
                anomalies.append(
                    _anomaly(
                        "invalid_observation",
                        f"observations[{index}]: {schema_error}",
                        relationship_id=rid or None,
                        extra={"observation_index": index},
                    )
                )
        # Count field.
        count_ok = (
            not isinstance(declared_count, bool)
            and isinstance(declared_count, (int, float))
            and not (
                isinstance(declared_count, float)
                and (math.isnan(declared_count) or math.isinf(declared_count))
            )
            and int(declared_count) == obs_count_out
            and float(declared_count) == float(obs_count_out)
        )
        if not count_ok:
            anomalies.append(
                _anomaly(
                    "observation_count_mismatch",
                    f"clang_type_use_observation_count={declared_count!r} "
                    f"!= decoded length {obs_count_out}",
                    relationship_id=rid or None,
                )
            )
        # Canonical producer order.
        try:
            ordered = sorted(observations, key=canonical_json)
            if [canonical_json(o) for o in observations] != [
                canonical_json(o) for o in ordered
            ]:
                anomalies.append(
                    _anomaly(
                        "non_canonical_observation_order",
                        "observations are not in canonical producer order",
                        relationship_id=rid or None,
                    )
                )
        except (TypeError, ValueError):
            anomalies.append(
                _anomaly(
                    "malformed_observations_json",
                    "observations are not JSON-serializable for order check",
                    relationship_id=rid or None,
                )
            )
        # use_kinds aggregate
        expected_kinds = sorted(
            {
                str(o.get("use_kind"))
                for o in observations
                if o.get("use_kind") is not None
            }
        )
        raw_kinds = _normalize_list_field(
            _row_get(rel, "clang_type_use_use_kinds")
        )
        if raw_kinds is None:
            anomalies.append(
                _anomaly(
                    "use_kinds_mismatch",
                    f"clang_type_use_use_kinds missing; expected {expected_kinds!r}",
                    relationship_id=rid or None,
                )
            )
        else:
            got_kinds = raw_kinds
            if (
                any(not isinstance(kind, str) for kind in got_kinds)
                or got_kinds != expected_kinds
            ):
                anomalies.append(
                    _anomaly(
                        "use_kinds_mismatch",
                        f"use_kinds={got_kinds!r} != expected {expected_kinds!r}",
                        relationship_id=rid or None,
                    )
                )
        # entry_indices aggregate
        expected_indices_set: Set[int] = set()
        indices_ok = True
        for o in observations:
            inds = o.get("entry_indices")
            if not isinstance(inds, list):
                indices_ok = False
                break
            for raw_i in inds:
                if isinstance(raw_i, bool) or not isinstance(raw_i, int):
                    indices_ok = False
                    break
                expected_indices_set.add(raw_i)
            if not indices_ok:
                break
        expected_indices = sorted(expected_indices_set)
        raw_indices = _normalize_list_field(
            _row_get(rel, "clang_type_use_entry_indices")
        )
        if not indices_ok:
            anomalies.append(
                _anomaly(
                    "entry_indices_mismatch",
                    "observation entry_indices are malformed",
                    relationship_id=rid or None,
                )
            )
        elif raw_indices is None:
            anomalies.append(
                _anomaly(
                    "entry_indices_mismatch",
                    f"clang_type_use_entry_indices missing; "
                    f"expected {expected_indices!r}",
                    relationship_id=rid or None,
                )
            )
        else:
            got_indices: Optional[List[int]] = None
            if all(
                not isinstance(index, bool)
                and isinstance(index, int)
                and index >= 0
                for index in raw_indices
            ):
                got_indices = list(raw_indices)
            if got_indices is None or got_indices != expected_indices:
                anomalies.append(
                    _anomaly(
                        "entry_indices_mismatch",
                        f"entry_indices={raw_indices!r} != "
                        f"expected {expected_indices!r}",
                        relationship_id=rid or None,
                    )
                )

    # Compilers + digest.
    digest = _row_get(rel, "clang_type_use_compile_commands_digest")
    if (
        not is_material_value(digest)
        or not isinstance(digest, str)
        or not digest.strip()
    ):
        if isinstance(digest, float) and (
            math.isnan(digest) or math.isinf(digest)
        ):
            anomalies.append(
                _anomaly(
                    "nan_or_infinity",
                    "compile_commands_digest is NaN/Infinity",
                    relationship_id=rid or None,
                )
            )
        else:
            anomalies.append(
                _anomaly(
                    "digest_mismatch",
                    f"compile_commands_digest missing/invalid: {digest!r}",
                    relationship_id=rid or None,
                )
            )
        digest_s = ""
    else:
        digest_s = digest.strip()

    compilers_raw = _row_get(rel, "clang_type_use_compilers")
    compilers: Optional[List[Dict[str, Any]]] = None
    if not is_material_value(compilers_raw):
        anomalies.append(
            _anomaly(
                "malformed_compilers_json",
                "clang_type_use_compilers is missing/null",
                relationship_id=rid or None,
            )
        )
    else:
        parsed_comp: Any = None
        non_finite_compilers = _contains_non_finite(compilers_raw)
        if isinstance(compilers_raw, str):
            try:
                parsed_comp = _strict_json_loads(compilers_raw)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                if "non-finite JSON constant" in str(error):
                    non_finite_compilers = True
                parsed_comp = None
        elif isinstance(compilers_raw, list):
            parsed_comp = compilers_raw
        else:
            parsed_comp = None
        if non_finite_compilers:
            anomalies.append(
                _anomaly(
                    "nan_or_infinity",
                    "clang_type_use_compilers contains NaN/Infinity",
                    relationship_id=rid or None,
                )
            )
        if (
            not isinstance(parsed_comp, list)
            or not parsed_comp
            or not all(isinstance(c, dict) for c in parsed_comp)
        ):
            anomalies.append(
                _anomaly(
                    "malformed_compilers_json",
                    "clang_type_use_compilers is not a non-empty JSON list of objects",
                    relationship_id=rid or None,
                )
            )
        else:
            compilers = []
            compiler_keys: List[Tuple[str, str, str]] = []
            for i, c in enumerate(parsed_comp):
                if set(c) != {
                    "compiler_path",
                    "compiler_id",
                    "compile_commands_digest",
                }:
                    anomalies.append(
                        _anomaly(
                            "malformed_compilers_json",
                            f"compilers[{i}] has unexpected fields "
                            f"{sorted(str(key) for key in c)!r}",
                            relationship_id=rid or None,
                        )
                    )
                path = c.get("compiler_path")
                cid = c.get("compiler_id")
                cdigest = c.get("compile_commands_digest")
                if (
                    not isinstance(path, str)
                    or not path.strip()
                    or not isinstance(cid, str)
                    or not cid.strip()
                    or not Path(path.strip()).is_absolute()
                ):
                    anomalies.append(
                        _anomaly(
                            "malformed_compilers_json",
                            f"compilers[{i}] has incomplete identity",
                            relationship_id=rid or None,
                        )
                    )
                    continue
                if not isinstance(cdigest, str) or not cdigest.strip():
                    anomalies.append(
                        _anomaly(
                            "malformed_compilers_json",
                            f"compilers[{i}] has invalid compile_commands_digest",
                            relationship_id=rid or None,
                        )
                    )
                    continue
                if digest_s and cdigest != digest_s:
                    anomalies.append(
                        _anomaly(
                            "digest_mismatch",
                            f"compilers[{i}].compile_commands_digest="
                            f"{cdigest!r} != edge digest {digest_s!r}",
                            relationship_id=rid or None,
                        )
                    )
                compilers.append(
                    {
                        "compiler_path": path.strip(),
                        "compiler_id": cid.strip(),
                        "compile_commands_digest": cdigest,
                    }
                )
                compiler_keys.append((path.strip(), cid.strip(), cdigest))
            if len(set(compiler_keys)) != len(compiler_keys):
                anomalies.append(
                    _anomaly(
                        "malformed_compilers_json",
                        "clang_type_use_compilers contains duplicate identities",
                        relationship_id=rid or None,
                    )
                )
            if compiler_keys != sorted(compiler_keys):
                anomalies.append(
                    _anomaly(
                        "malformed_compilers_json",
                        "clang_type_use_compilers is not in producer canonical order",
                        relationship_id=rid or None,
                    )
                )

    if compilers is not None:
        cpath = _row_get(rel, "clang_type_use_compiler_path")
        cid = _row_get(rel, "clang_type_use_compiler_id")
        if len(compilers) == 1:
            only = compilers[0]
            if (
                not is_material_value(cpath)
                or not is_material_value(cid)
                or str(cpath) != only["compiler_path"]
                or str(cid) != only["compiler_id"]
            ):
                anomalies.append(
                    _anomaly(
                        "compiler_shortcut_mismatch",
                        "single-compiler shortcut fields disagree with compilers list",
                        relationship_id=rid or None,
                        extra={
                            "compiler_path": str(cpath),
                            "compiler_id": str(cid),
                        },
                    )
                )
        else:
            if is_material_value(cpath) or is_material_value(cid):
                anomalies.append(
                    _anomaly(
                        "compiler_shortcut_mismatch",
                        "multi-compiler edge exposes singular compiler shortcut fields",
                        relationship_id=rid or None,
                    )
                )

    return anomalies, obs_count_out


def validate_persisted_type_use_overlay(
    entities: Any,
    relationships: Any,
    manifest: Optional[Mappingish] = None,
    *,
    max_anomaly_samples: int = _MAX_ANOMALY_SAMPLES,
) -> Dict[str, Any]:
    """Validate already-persisted configured ``uses_type`` relationships.

    Pure and non-mutating. Never invokes Clang, reindexes, or rewrites graphs.

    Compatibility modes:
      * no ``clang_type_uses`` block and zero configured edges → ``legacy_absent``
      * ``mode=off`` / ``enabled=false`` → requires zero configured edges
      * enabled ``configured_clang_type_uses`` → full edge + manifest census

    A missing manifest block never legitimizes existing configured edges.
    """
    if max_anomaly_samples < 0:
        max_anomaly_samples = 0

    entities_list = _table_rows(entities, name="entities")
    relationships_list = _table_rows(
        relationships, name="relationships"
    )
    manifest_obj: Dict[str, Any] = {}
    if manifest is not None:
        if not isinstance(manifest, dict) and hasattr(manifest, "items"):
            manifest_obj = dict(manifest)
        elif isinstance(manifest, dict):
            manifest_obj = manifest
        else:
            raise TypeError("manifest must be a mapping or None")

    anomalies: List[Dict[str, Any]] = []

    # Index entities by title and id (read-only).
    by_title: Dict[str, List[Dict[str, Any]]] = {}
    by_id: Dict[str, Dict[str, Any]] = {}
    for e in entities_list:
        title = str(e.get("title") or "")
        eid = str(e.get("id") or "")
        if title:
            by_title.setdefault(title, []).append(e)
        if eid:
            # Last write wins for id map; duplicate ids are not a type-use
            # contract (producer refuses them at overlay time). Title
            # resolution still uses the full multi-map.
            by_id[eid] = e

    # Global relationship-id uniqueness (all relationships).
    seen_ids: Dict[str, int] = {}
    for idx, rel in enumerate(relationships_list):
        rid = _row_get(rel, "id")
        if not is_material_value(rid) or not str(rid).strip():
            anomalies.append(
                _anomaly(
                    "empty_relationship_id",
                    f"relationship at index {idx} has empty id",
                )
            )
            continue
        rid_s = str(rid)
        if rid_s in seen_ids:
            anomalies.append(
                _anomaly(
                    "duplicate_relationship_id",
                    f"duplicate relationship id {rid_s!r}",
                    relationship_id=rid_s,
                    extra={"other_index": seen_ids[rid_s]},
                )
            )
        else:
            seen_ids[rid_s] = idx

    # Classify configured edges and stale metadata.
    configured: List[Dict[str, Any]] = []
    for rel in relationships_list:
        rel_type = str(_row_get(rel, "type") or "")
        fact = str(_row_get(rel, "fact_kind") or "")
        rid = str(_row_get(rel, "id") or "") or None
        has_tu = _has_material_type_use_fields(rel)
        if rel_type != REL_TYPE:
            if has_tu:
                anomalies.append(
                    _anomaly(
                        "stale_type_use_metadata",
                        f"non-uses_type relationship has material clang_type_use_* "
                        f"fields (type={rel_type!r})",
                        relationship_id=rid,
                    )
                )
            continue
        # uses_type relationship.
        if fact == FACT_KIND:
            configured.append(rel)
        else:
            anomalies.append(
                _anomaly(
                    "non_configured_uses_type",
                    f"uses_type relationship has fact_kind={fact!r}, "
                    f"expected {FACT_KIND!r}",
                    relationship_id=rid,
                )
            )
            if has_tu:
                # Still inspect unknown fields etc. via configured path would
                # be wrong; flag as non-configured only.
                pass

    block = manifest_obj.get("clang_type_uses")
    has_block = block is not None
    mode_state = "legacy_absent"
    block_enabled = False

    if not has_block:
        if configured:
            anomalies.append(
                _anomaly(
                    "legacy_block_missing_with_edges",
                    f"manifest lacks clang_type_uses but graph has "
                    f"{len(configured)} configured uses_type edge(s)",
                    extra={"n_edges": len(configured)},
                )
            )
            mode_state = "invalid"
        else:
            mode_state = "legacy_absent"
    else:
        if not isinstance(block, dict):
            anomalies.append(
                _anomaly(
                    "invalid_enabled_block",
                    f"clang_type_uses manifest block is not an object: "
                    f"{type(block).__name__}",
                )
            )
            mode_state = "invalid"
        else:
            mode = block.get("mode")
            enabled = block.get("enabled")
            if mode == "off" and enabled is False:
                mode_state = "off"
                block_enabled = False
                if configured:
                    anomalies.append(
                        _anomaly(
                            "off_with_configured_edges",
                            f"clang_type_uses is off/disabled but graph has "
                            f"{len(configured)} configured uses_type edge(s)",
                            extra={"n_edges": len(configured)},
                        )
                    )
                # Off block should not claim nonzero facts.
                disabled_counts = {
                    key: block.get(key)
                    for key in (
                        "n_facts",
                        "n_observations",
                        "n_translation_units",
                    )
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
                            f"off block has invalid zero census "
                            f"{disabled_counts!r}",
                        )
                    )
            elif mode == MODE and enabled is True:
                mode_state = "enabled"
                block_enabled = True
            elif (
                mode == "off"
                or mode == MODE
                or enabled is True
                or enabled is False
            ):
                # Partial/inconsistent enablement.
                anomalies.append(
                    _anomaly(
                        "invalid_enabled_block",
                        f"clang_type_uses enablement inconsistent: "
                        f"mode={mode!r} enabled={enabled!r}",
                    )
                )
                mode_state = "invalid"
                # If it looks enabled-ish, still validate edges.
                block_enabled = bool(enabled) or mode == MODE
            else:
                anomalies.append(
                    _anomaly(
                        "invalid_enabled_block",
                        f"unknown clang_type_uses mode={mode!r} enabled={enabled!r}",
                    )
                )
                mode_state = "invalid"

    total_observations = 0
    pair_index: Dict[Tuple[str, str], str] = {}
    if block_enabled or (mode_state == "invalid" and configured):
        # Full edge validation when enabled (or when edges exist without a
        # legitimate off/legacy state).
        for rel in sorted(
            configured,
            key=lambda r: (
                str(_row_get(r, "source") or ""),
                str(_row_get(r, "target") or ""),
                str(_row_get(r, "id") or ""),
            ),
        ):
            edge_anoms, obs_n = _validate_one_configured_edge(
                rel,
                by_title=by_title,
                by_id=by_id,
                pair_index=pair_index,
            )
            anomalies.extend(edge_anoms)
            total_observations += obs_n
    elif configured and mode_state == "off":
        # Off with edges already flagged; still do not invent residual
        # counts from partial edge inspection beyond the off anomaly.
        pass

    # Manifest cross-check for enabled graphs.
    if block_enabled and isinstance(block, dict):
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
        if block.get("fact_kind") != FACT_KIND:
            anomalies.append(
                _anomaly(
                    "manifest_identity_mismatch",
                    f"manifest fact_kind={block.get('fact_kind')!r} "
                    f"expected {FACT_KIND!r}",
                )
            )
        if block.get("extractor") != EXTRACTOR:
            anomalies.append(
                _anomaly(
                    "manifest_identity_mismatch",
                    f"manifest extractor={block.get('extractor')!r} "
                    f"expected {EXTRACTOR!r}",
                )
            )
        n_facts = block.get("n_facts")
        if (
            isinstance(n_facts, bool)
            or not isinstance(n_facts, int)
            or n_facts != len(configured)
        ):
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"manifest n_facts={n_facts!r} != configured edge count "
                    f"{len(configured)}",
                )
            )
        n_obs = block.get("n_observations")
        if (
            isinstance(n_obs, bool)
            or not isinstance(n_obs, int)
            or n_obs != total_observations
        ):
            # Only flag when we successfully decoded edges; if every edge
            # failed to decode, total_observations may be 0 while n_obs > 0 —
            # still a real mismatch.
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"manifest n_observations={n_obs!r} != sum of decoded "
                    f"observations {total_observations}",
                )
            )
        # Digest consistency across edges and manifest.
        m_digest = block.get("compile_commands_digest")
        if not isinstance(m_digest, str) or not m_digest.strip():
            anomalies.append(
                _anomaly(
                    "digest_mismatch",
                    f"manifest compile_commands_digest missing: {m_digest!r}",
                )
            )
        else:
            m_digest_s = m_digest.strip()
            for rel in configured:
                edge_d = _row_get(rel, "clang_type_use_compile_commands_digest")
                if is_material_value(edge_d) and str(edge_d) != m_digest_s:
                    anomalies.append(
                        _anomaly(
                            "digest_mismatch",
                            f"edge digest {edge_d!r} != manifest {m_digest_s!r}",
                            relationship_id=str(_row_get(rel, "id") or "")
                            or None,
                        )
                    )
        # Compiler census and singular shortcuts are producer identity.
        compilers_block = block.get("compilers")
        manifest_compiler_ids: Set[Tuple[str, str]] = set()
        valid_manifest_compilers: List[Dict[str, Any]] = []
        if (
            not isinstance(compilers_block, list)
            or not compilers_block
            or not all(isinstance(compiler, dict) for compiler in compilers_block)
            or _contains_non_finite(compilers_block)
        ):
            anomalies.append(
                _anomaly(
                    "manifest_identity_mismatch",
                    "manifest compilers must be a non-empty finite list of objects",
                )
            )
        else:
            for index, compiler in enumerate(compilers_block):
                path = compiler.get("compiler_path")
                compiler_id = compiler.get("compiler_id")
                if (
                    not isinstance(path, str)
                    or not path.strip()
                    or not Path(path.strip()).is_absolute()
                    or not isinstance(compiler_id, str)
                    or not compiler_id.strip()
                ):
                    anomalies.append(
                        _anomaly(
                            "manifest_identity_mismatch",
                            f"manifest compilers[{index}] has incomplete identity",
                        )
                    )
                    continue
                identity = (path.strip(), compiler_id.strip())
                if identity in manifest_compiler_ids:
                    anomalies.append(
                        _anomaly(
                            "manifest_identity_mismatch",
                            f"manifest compilers contains duplicate {identity!r}",
                        )
                    )
                    continue
                manifest_compiler_ids.add(identity)
                valid_manifest_compilers.append(compiler)
            try:
                encoded_compilers = [
                    canonical_json(compiler) for compiler in compilers_block
                ]
                if encoded_compilers != sorted(encoded_compilers):
                    anomalies.append(
                        _anomaly(
                            "manifest_identity_mismatch",
                            "manifest compilers is not in producer canonical order",
                        )
                    )
            except (TypeError, ValueError):
                anomalies.append(
                    _anomaly(
                        "manifest_identity_mismatch",
                        "manifest compilers is not canonical JSON evidence",
                    )
                )

        if len(valid_manifest_compilers) == 1:
            only = valid_manifest_compilers[0]
            for field in ("compiler_path", "compiler_id"):
                if block.get(field) != only.get(field):
                    anomalies.append(
                        _anomaly(
                            "manifest_identity_mismatch",
                            f"manifest {field}={block.get(field)!r} != "
                            f"compilers[0].{field}={only.get(field)!r}",
                        )
                    )
            if (
                block.get("compiler_version") is not None
                and block.get("compiler_version") != only.get("compiler_version")
            ):
                anomalies.append(
                    _anomaly(
                        "manifest_identity_mismatch",
                        "manifest compiler_version disagrees with compilers[0]",
                    )
                )
        elif len(valid_manifest_compilers) > 1:
            for field in ("compiler_path", "compiler_id", "compiler_version"):
                if block.get(field) is not None:
                    anomalies.append(
                        _anomaly(
                            "manifest_identity_mismatch",
                            f"multi-compiler manifest exposes singular "
                            f"{field}={block.get(field)!r}",
                        )
                    )

        for rel in configured:
            raw_edge_compilers = _row_get(rel, "clang_type_use_compilers")
            try:
                edge_compilers = (
                    _strict_json_loads(raw_edge_compilers)
                    if isinstance(raw_edge_compilers, str)
                    else raw_edge_compilers
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(edge_compilers, list):
                continue
            for compiler in edge_compilers:
                if not isinstance(compiler, dict):
                    continue
                identity = (
                    str(compiler.get("compiler_path") or "").strip(),
                    str(compiler.get("compiler_id") or "").strip(),
                )
                if all(identity) and identity not in manifest_compiler_ids:
                    anomalies.append(
                        _anomaly(
                            "manifest_identity_mismatch",
                            f"edge compiler {identity!r} absent from manifest census",
                            relationship_id=str(_row_get(rel, "id") or "") or None,
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
        # Confidence boundary is part of the persisted producer contract.
        cb = block.get("confidence_boundary")
        if cb != CONFIDENCE_BOUNDARY:
            anomalies.append(
                _anomaly(
                    "confidence_boundary",
                    "manifest confidence_boundary differs from producer contract",
                )
            )
        # Relationship table count vs manifest counts.relationships when present.
        counts = manifest_obj.get("counts")
        declared_rels = counts.get("relationships") if isinstance(counts, dict) else None
        if (
            isinstance(declared_rels, bool)
            or not isinstance(declared_rels, int)
            or declared_rels != len(relationships_list)
        ):
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"manifest counts.relationships={declared_rels!r} "
                    f"!= relationship table length {len(relationships_list)}",
                )
            )

    # Deterministic anomaly ordering.
    anomalies.sort(
        key=lambda a: (
            str(a.get("code") or ""),
            str(a.get("relationship_id") or ""),
            str(a.get("message") or ""),
            canonical_json(
                {k: a[k] for k in sorted(a) if k not in {"code", "message", "relationship_id"}}
            ),
        )
    )
    total = len(anomalies)
    samples = anomalies[:max_anomaly_samples]
    ok = total == 0 and mode_state in {"legacy_absent", "off", "enabled"}
    # enabled with zero edges is valid (empty overlay after clean report).
    status = mode_state if ok or mode_state != "invalid" else "invalid"
    if not ok and mode_state in {"legacy_absent", "off", "enabled"}:
        status = "invalid"

    return {
        "ok": ok,
        "status": status,
        "mode": mode_state,
        "n_configured_edges": len(configured),
        "n_observations_decoded": total_observations,
        "n_anomalies": total,
        "n_anomaly_samples": len(samples),
        "anomalies_truncated": total > len(samples),
        "anomalies": samples,
        "confidence_boundary": CONFIDENCE_BOUNDARY,
        "fact_kind": FACT_KIND,
        "extractor": EXTRACTOR,
        "relationship_type": REL_TYPE,
    }
