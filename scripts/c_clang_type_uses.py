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

_TYPE_USE_FIELDS = (
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


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


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


def _relationship_id(
    source_title: str,
    target_title: str,
    source_entity_id: str,
    target_entity_id: str,
) -> str:
    slug_src = re.sub(r"[^0-9A-Za-z_.]", "_", source_title)
    slug_tgt = re.sub(r"[^0-9A-Za-z_.]", "_", target_title)
    edge_digest = hashlib.sha256(
        (
            f"{source_entity_id}\0{target_entity_id}\0{source_title}\0"
            f"{target_title}\0{FACT_KIND}"
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"rel:uses_type:{slug_src}->{slug_tgt}:{edge_digest}"


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
