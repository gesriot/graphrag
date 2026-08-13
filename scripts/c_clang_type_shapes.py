#!/usr/bin/env python
"""Optional Clang configured type-shape evidence overlay.

Publishes configuration/toolchain-derived **ordered direct member-name**
evidence onto *existing* tree-sitter-c ``struct`` / ``enum`` entities, taken
only from ``build_type_shape_audit_from_capture`` ``matched_shape`` rows.

This is **not**:
  * ABI, layout, size, alignment, offset, or calling-convention evidence
  * FFI-safety or Rust ``repr`` compatibility proof
  * new entities, relationships, ``uses_type`` edges, or alternate-site entities
  * multi-config or C++ coverage
  * silent application of residual or non-unique audit rows

Hard equality is the ordered list of direct member names. Clang ``qualType``,
``desugaredQualType``, enum values, bit-field widths, and member locations are
published as diagnostic evidence relative to the recorded Clang +
``compile_commands.json`` configuration, never as layout equality.

Attachment is collision-safe: an audit row is applied only when exactly one
graph entity agrees on entity type, ``tree_sitter_title``, ``symbol_name``,
package-relative source path, and canonical graph span. The graph canonical
span comes from the type-declaration audit that produced the shape owners, so
the two reports must describe the same capture, digest, and toolchain.
Validation is atomic: any error leaves ``data`` unchanged.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from c_clang_type_shape_audit import (  # type: ignore
    ClangTypeShapeAuditError,
)
from c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    build_disabled_overlay_provenance,
)
from c_identities import package_relative_posix  # type: ignore

MODE = "configured_clang_type_shapes"
FACT_KIND = "configured_type_shape"
EXTRACTOR = "clang-ast-json"
SHAPE_ENTITY_KINDS = frozenset({"struct", "enum"})
SHAPE_AUDIT_MODE = "clang_ast_json_type_shape_audit"
TYPE_AUDIT_MODE = "clang_ast_json_type_declaration_audit"

CONFIDENCE_BOUNDARY = (
    "clang_shape_confidence=1.0 and clang_shape_is_deterministic=true mean the "
    "ordered direct member *names* of this struct/enum declaration are "
    "re-derivable from the recorded Clang + compile_commands.json "
    "configuration via the AST type-shape audit. They do not mean size, "
    "alignment, offsets, calling convention, ABI or layout equality, FFI "
    "safety, Rust representation compatibility, multi-config coverage, or C++ "
    "support. clang_shape_member_evidence (qualType, desugaredQualType, enum "
    "values, bit-field widths, member locations) is configuration-relative "
    "diagnostic evidence only. Typedef aliases are not independent shapes and "
    "nested record bodies are not flattened. Residual shape buckets "
    "(tree_sitter_only_members, clang_only_members, member_order_mismatch, "
    "duplicate_or_ambiguous_members, macro_location_unsupported, "
    "owner_unmatched) fail closed; unsupported_member_form and "
    "outside_package_declarations stay observation-only and receive no "
    "metadata."
)

HARD_EQUALITY = "ordered direct member names only"

# Diagnostic member fields that are never equality/ABI claims.
EVIDENCE_ONLY = (
    "qualType",
    "desugaredQualType",
    "enum_value",
    "bit_width",
    "locations",
)

# Substrings every published clang_shape_description must keep, so a persisted
# field cannot silently drop its evidence boundary.
DESCRIPTION_REQUIRED_SUBSTRINGS = (
    HARD_EQUALITY,
    "not ABI or layout equality",
    "deterministic only relative to recorded Clang + compile_commands.json",
)

LIMITATIONS = (
    "Hard equality is ordered direct member names only",
    "Not ABI evidence",
    "Not layout/size/alignment/offset evidence",
    "Not FFI-safety proof",
    "Not Rust representation (repr) proof",
    "Not multi-config: one recorded compile_commands.json configuration only",
    "Not C++",
    "Only struct/enum owners already matched by the type-declaration audit",
    "Typedef aliases are not independent shapes; nested records not flattened",
    "No new entities, relationships, uses_type edges, or alternate-site entities",
)

# Fields written onto struct/enum entities (scalar / list / JSON text).
_SHAPE_FIELDS = (
    "clang_shape_members_validated",
    "clang_shape_fact_kind",
    "clang_shape_extractor",
    "clang_shape_entity_kind",
    "clang_shape_member_count",
    "clang_shape_member_names",
    "clang_shape_member_evidence",
    "clang_shape_graph_canonical_span",
    "clang_shape_matched_site_span",
    "clang_shape_matched_site_line",
    "clang_shape_matched_site_col0",
    "clang_shape_matched_site_is_canonical",
    "clang_shape_location_origin",
    "clang_shape_entry_indices",
    "clang_shape_compiler_path",
    "clang_shape_compiler_id",
    "clang_shape_compilers",
    "clang_shape_compile_commands_digest",
    "clang_shape_confidence",
    "clang_shape_is_deterministic",
    "clang_shape_description",
)

# Any nonzero residual here blocks publication of the whole overlay.
_FAIL_CLOSED_BUCKETS = (
    "tree_sitter_only_members",
    "clang_only_members",
    "member_order_mismatch",
    "duplicate_or_ambiguous_members",
    "macro_location_unsupported",
    "owner_unmatched",
)

# Observation-only residuals: counted in the manifest, never decorated.
_OBSERVATION_ONLY_BUCKETS = (
    "unsupported_member_form",
    "outside_package_declarations",
)

_ALL_REPORT_BUCKETS = (
    "matched_shape",
    *_FAIL_CLOSED_BUCKETS,
    *_OBSERVATION_ONLY_BUCKETS,
)

# Derived audit counts republished in the manifest for census transparency.
_DERIVED_COUNTS = (
    "type_declaration_matched_struct_enum",
    "type_declaration_matched_total",
    "shape_owners_classified",
)

# Clang member evidence keys published verbatim (diagnostic only).
_MEMBER_EVIDENCE_KEYS = (
    "name",
    "order",
    "form",
    "is_bitfield",
    "bit_width",
    "enum_value",
    "qualType",
    "desugaredQualType",
    "line",
    "col0",
)


class ClangTypeShapeOverlayError(CompilerOverlayError):
    """Raised when the type-shape overlay cannot apply honestly."""


def build_disabled_provenance() -> Dict[str, Any]:
    """Stable manifest block when ``--clang-type-shapes`` is off."""
    return build_disabled_overlay_provenance()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))


def _values_compatible(existing: Any, new: Any) -> bool:
    if _is_missing(existing):
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


def _entity_rel_source(entity: Dict[str, Any], package_dir: Path) -> str:
    raw = str(entity.get("source_file") or "")
    if not raw:
        raise ClangTypeShapeOverlayError(
            f"entity {entity.get('title')!r} has empty source_file"
        )
    p = Path(raw)
    p = (package_dir / p).resolve() if not p.is_absolute() else p.resolve()
    try:
        return package_relative_posix(p, package_dir)
    except (OSError, ValueError) as e:
        raise ClangTypeShapeOverlayError(
            f"cannot normalize source_file {raw!r} for entity "
            f"{entity.get('title')!r}: {e}"
        ) from e


def _non_empty_str(value: Any, *, field: str, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClangTypeShapeOverlayError(f"{context} has empty {field}")
    return value.strip()


def _coordinate(value: Any, *, field: str, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClangTypeShapeOverlayError(
            f"{context} has invalid {field} {value!r}"
        )
    if field.endswith("line") and value < 1:
        raise ClangTypeShapeOverlayError(
            f"{context} has non-positive {field}"
        )
    return value


@dataclass(frozen=True)
class _Provenance:
    """Validated report-level compile/toolchain census."""

    digest: str
    n_compile_entries: int
    n_translation_units: int
    allowed_compilers: Set[Tuple[str, str]]
    entry_compilers: Dict[int, Tuple[str, str]]
    normalized_compilers: List[Dict[str, Any]]
    singular: Tuple[Any, Any, Any]

    def identity(self) -> Tuple[Any, ...]:
        return (
            self.digest,
            self.n_compile_entries,
            tuple(_canonical_json(row) for row in self.normalized_compilers),
            tuple(sorted(self.entry_compilers.items())),
            self.singular,
        )


def _validate_report_provenance(
    report: Dict[str, Any],
    package_dir: Path,
    *,
    context: str,
    expected_mode: str,
) -> _Provenance:
    """Validate mode, package, digest, compile-entry and toolchain census."""
    if not isinstance(report, dict):
        raise ClangTypeShapeOverlayError(f"{context} is not an object")
    if str(report.get("mode") or "") != expected_mode:
        raise ClangTypeShapeOverlayError(
            f"unexpected {context} mode {report.get('mode')!r}; expected "
            f"{expected_mode}"
        )
    if str(report.get("package") or "") != package_dir.name:
        raise ClangTypeShapeOverlayError(
            f"{context} package {report.get('package')!r} does not match the "
            f"target package {package_dir.name!r}"
        )
    digest = _non_empty_str(
        report.get("compile_commands_digest"),
        field="compile_commands_digest",
        context=context,
    )

    n_compile_entries = report.get("n_compile_entries")
    if (
        isinstance(n_compile_entries, bool)
        or not isinstance(n_compile_entries, int)
        or n_compile_entries <= 0
    ):
        raise ClangTypeShapeOverlayError(
            f"{context} n_compile_entries must be a positive integer"
        )
    translation_units = report.get("translation_units")
    if (
        not isinstance(translation_units, list)
        or not all(isinstance(row, dict) for row in translation_units)
        or len(translation_units) != n_compile_entries
    ):
        raise ClangTypeShapeOverlayError(
            f"{context} translation_units must contain exactly one row per "
            "compile entry"
        )

    compilers = report.get("compilers")
    if (
        not isinstance(compilers, list)
        or not compilers
        or not all(isinstance(row, dict) for row in compilers)
    ):
        raise ClangTypeShapeOverlayError(
            f"{context} compilers must be a non-empty list of objects"
        )
    allowed_compilers: Set[Tuple[str, str]] = set()
    normalized_compilers: List[Dict[str, Any]] = []
    for position, compiler in enumerate(compilers):
        cctx = f"{context}.compilers[{position}]"
        compiler_path = _non_empty_str(
            compiler.get("compiler_path"), field="compiler_path", context=cctx
        )
        compiler_id = _non_empty_str(
            compiler.get("compiler_id"), field="compiler_id", context=cctx
        )
        if not Path(compiler_path).is_absolute():
            raise ClangTypeShapeOverlayError(
                f"{cctx} compiler_path is not absolute: {compiler_path!r}"
            )
        identity = (compiler_path, compiler_id)
        if identity in allowed_compilers:
            raise ClangTypeShapeOverlayError(
                f"duplicate compiler identity in {context}.compilers: "
                f"{identity!r}"
            )
        allowed_compilers.add(identity)
        normalized = dict(compiler)
        normalized["compiler_path"] = compiler_path
        normalized["compiler_id"] = compiler_id
        normalized_compilers.append(normalized)
    try:
        normalized_compilers.sort(key=_canonical_json)
    except (TypeError, ValueError) as e:
        raise ClangTypeShapeOverlayError(
            f"{context} compiler provenance is not JSON-serializable: {e}"
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
            raise ClangTypeShapeOverlayError(
                f"{context} translation unit {position} has invalid or "
                f"duplicate entry_index {entry_index!r}"
            )
        tu_path = translation_unit.get("compiler_path")
        tu_id = translation_unit.get("compiler_id")
        if not isinstance(tu_path, str) or not isinstance(tu_id, str):
            raise ClangTypeShapeOverlayError(
                f"{context} translation unit {position} has incomplete "
                "compiler identity"
            )
        identity = (tu_path.strip(), tu_id.strip())
        if identity not in allowed_compilers:
            raise ClangTypeShapeOverlayError(
                f"{context} translation unit {position} names a compiler "
                "absent from report.compilers"
            )
        entry_compilers[entry_index] = identity
    if set(entry_compilers) != set(range(n_compile_entries)):
        raise ClangTypeShapeOverlayError(
            f"{context} translation-unit entry indices are not a complete "
            "census"
        )

    top_path = report.get("compiler_path")
    top_id = report.get("compiler_id")
    top_version = report.get("compiler_version")
    if len(allowed_compilers) == 1:
        only_path, only_id = next(iter(allowed_compilers))
        only_version = normalized_compilers[0].get("compiler_version")
        if (top_path, top_id) != (only_path, only_id):
            raise ClangTypeShapeOverlayError(
                f"{context} singular compiler identity disagrees with "
                "report.compilers"
            )
        if top_version is not None and top_version != only_version:
            raise ClangTypeShapeOverlayError(
                f"{context} singular compiler_version disagrees with "
                "report.compilers"
            )
    elif any(value is not None for value in (top_path, top_id, top_version)):
        raise ClangTypeShapeOverlayError(
            f"multi-compiler {context} must not expose a singular compiler "
            "identity"
        )

    return _Provenance(
        digest=digest,
        n_compile_entries=n_compile_entries,
        n_translation_units=len(translation_units),
        allowed_compilers=allowed_compilers,
        entry_compilers=entry_compilers,
        normalized_compilers=normalized_compilers,
        singular=(top_path, top_id, top_version),
    )


def _validated_bucket_counts(
    report: Dict[str, Any],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    counts_raw = report.get("counts")
    if not isinstance(counts_raw, dict):
        raise ClangTypeShapeOverlayError(
            "type-shape audit report has no counts object"
        )
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    counts: Dict[str, int] = {}
    for key in _ALL_REPORT_BUCKETS:
        rows = report.get(key)
        if not isinstance(rows, list) or not all(
            isinstance(row, dict) for row in rows
        ):
            raise ClangTypeShapeOverlayError(
                f"type-shape audit report bucket {key!r} must be a list of "
                "objects"
            )
        raw_count = counts_raw.get(key)
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 0
        ):
            raise ClangTypeShapeOverlayError(
                f"type-shape audit count {key!r} must be a non-negative integer"
            )
        if raw_count != len(rows):
            raise ClangTypeShapeOverlayError(
                f"type-shape audit count/list mismatch for {key!r}: "
                f"count={raw_count} rows={len(rows)}"
            )
        buckets[key] = list(rows)
        counts[key] = raw_count
    for key in _DERIVED_COUNTS:
        raw_count = counts_raw.get(key)
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 0
        ):
            raise ClangTypeShapeOverlayError(
                f"type-shape audit count {key!r} must be a non-negative integer"
            )
        counts[key] = raw_count
    return buckets, counts


def _fail_closed_residuals(buckets: Dict[str, List[Dict[str, Any]]]) -> None:
    problems = [
        f"{key}={len(buckets[key])}"
        for key in _FAIL_CLOSED_BUCKETS
        if buckets[key]
    ]
    if problems:
        raise ClangTypeShapeOverlayError(
            "clang type-shape overlay refuses unclean shape residuals: "
            + ", ".join(problems)
            + "; resolve or leave --clang-type-shapes off"
        )


def _index_shape_owners(type_report: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map tree_sitter_title -> matched struct/enum type-declaration row."""
    matched = type_report.get("matched")
    if not isinstance(matched, list) or not all(
        isinstance(row, dict) for row in matched
    ):
        raise ClangTypeShapeOverlayError(
            "type-declaration report matched bucket must be a list of objects"
        )
    by_title: Dict[str, Dict[str, Any]] = {}
    for row in matched:
        if str(row.get("entity_kind") or "") not in SHAPE_ENTITY_KINDS:
            continue
        title = str(row.get("tree_sitter_title") or "")
        if not title:
            raise ClangTypeShapeOverlayError(
                "type-declaration matched row missing tree_sitter_title"
            )
        if title in by_title:
            raise ClangTypeShapeOverlayError(
                f"type-declaration report has duplicate matched title {title!r}"
            )
        by_title[title] = row
    return by_title


def _member_names(value: Any, *, field: str, context: str) -> List[str]:
    if not isinstance(value, list):
        raise ClangTypeShapeOverlayError(f"{context} has non-list {field}")
    out: List[str] = []
    for name in value:
        if not isinstance(name, str) or not name:
            raise ClangTypeShapeOverlayError(
                f"{context} has an empty or non-string entry in {field}"
            )
        out.append(name)
    if len(set(out)) != len(out):
        raise ClangTypeShapeOverlayError(
            f"{context} has duplicate names in {field}"
        )
    return out


def _member_evidence(
    members: Any, names: Sequence[str], *, context: str
) -> str:
    """Canonical JSON evidence for the ordered, fully named Clang members."""
    if not isinstance(members, list) or not all(
        isinstance(m, dict) for m in members
    ):
        raise ClangTypeShapeOverlayError(
            f"{context} clang_members must be a list of objects"
        )
    if len(members) != len(names):
        raise ClangTypeShapeOverlayError(
            f"{context} clang_members census {len(members)} disagrees with "
            f"{len(names)} published member names"
        )
    evidence: List[Dict[str, Any]] = []
    for position, member in enumerate(members):
        if not _is_missing(member.get("residual")):
            raise ClangTypeShapeOverlayError(
                f"{context} member {position} carries residual "
                f"{member.get('residual')!r} inside a matched shape"
            )
        if member.get("name") != names[position]:
            raise ClangTypeShapeOverlayError(
                f"{context} member {position} name {member.get('name')!r} "
                f"disagrees with published order"
            )
        if member.get("order") != position:
            raise ClangTypeShapeOverlayError(
                f"{context} member {position} has order {member.get('order')!r}"
            )
        evidence.append({key: member.get(key) for key in _MEMBER_EVIDENCE_KEYS})
    try:
        return _canonical_json(evidence)
    except (TypeError, ValueError) as e:
        raise ClangTypeShapeOverlayError(
            f"{context} member evidence is not JSON-serializable: {e}"
        ) from e


def _validate_tree_members(
    members: Any, names: Sequence[str], *, context: str
) -> None:
    """Bind the published tree-sitter name list to its raw member inventory."""
    if not isinstance(members, list) or not all(
        isinstance(member, dict) for member in members
    ):
        raise ClangTypeShapeOverlayError(
            f"{context} tree_sitter_members must be a list of objects"
        )
    if len(members) != len(names):
        raise ClangTypeShapeOverlayError(
            f"{context} tree_sitter_members census disagrees with published "
            "member names"
        )
    for position, member in enumerate(members):
        if not _is_missing(member.get("residual")):
            raise ClangTypeShapeOverlayError(
                f"{context} tree-sitter member {position} carries residual "
                f"{member.get('residual')!r} inside a matched shape"
            )
        if member.get("name") != names[position]:
            raise ClangTypeShapeOverlayError(
                f"{context} tree-sitter member {position} name "
                f"{member.get('name')!r} disagrees with published order"
            )
        if member.get("order") != position:
            raise ClangTypeShapeOverlayError(
                f"{context} tree-sitter member {position} has order "
                f"{member.get('order')!r}"
            )


def _row_entry_indices(
    value: Any, *, context: str, n_compile_entries: int
) -> List[int]:
    if not isinstance(value, list) or not value:
        raise ClangTypeShapeOverlayError(
            f"{context} must have a non-empty list entry_indices"
        )
    seen: Set[int] = set()
    for raw in value:
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int)
            or raw < 0
            or raw >= n_compile_entries
        ):
            raise ClangTypeShapeOverlayError(
                f"{context} has invalid compile entry index {raw!r}"
            )
        if raw in seen:
            raise ClangTypeShapeOverlayError(
                f"{context} contains duplicate entry indices"
            )
        seen.add(raw)
    if value != sorted(seen):
        raise ClangTypeShapeOverlayError(
            f"{context} entry_indices must be sorted and unique"
        )
    return list(value)


def _row_compilers_json(
    value: Any,
    *,
    context: str,
    expected_digest: str,
    allowed_compilers: Set[Tuple[str, str]],
) -> str:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(c, dict) for c in value)
    ):
        raise ClangTypeShapeOverlayError(
            f"{context} compilers must be a non-empty list of objects"
        )
    normalized: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for position, compiler in enumerate(value):
        cctx = f"{context}.compilers[{position}]"
        path = _non_empty_str(
            compiler.get("compiler_path"), field="compiler_path", context=cctx
        )
        cid = _non_empty_str(
            compiler.get("compiler_id"), field="compiler_id", context=cctx
        )
        if compiler.get("compile_commands_digest") != expected_digest:
            raise ClangTypeShapeOverlayError(
                f"{cctx} compile_commands_digest disagrees with the audit report"
            )
        identity = (path, cid)
        if identity not in allowed_compilers:
            raise ClangTypeShapeOverlayError(
                f"{cctx} names a compiler absent from report.compilers"
            )
        if identity in seen:
            raise ClangTypeShapeOverlayError(
                f"{context} compilers contains a duplicate identity"
            )
        seen.add(identity)
        normalized.append(
            {
                "compiler_path": path,
                "compiler_id": cid,
                "compile_commands_digest": expected_digest,
            }
        )
    normalized.sort(key=_canonical_json)
    return _canonical_json(normalized)


def _shape_payload(
    row: Dict[str, Any],
    owner: Dict[str, Any],
    *,
    provenance: _Provenance,
) -> Dict[str, Any]:
    """Build the entity field dict for one matched_shape row."""
    title = str(row.get("tree_sitter_title") or "")
    context = f"matched_shape row {title!r}"
    kind = row.get("entity_kind")
    if kind not in SHAPE_ENTITY_KINDS:
        raise ClangTypeShapeOverlayError(
            f"{context} has invalid entity_kind {kind!r}"
        )

    names = _member_names(
        row.get("clang_member_names"), field="clang_member_names", context=context
    )
    tree_names = _member_names(
        row.get("tree_sitter_member_names"),
        field="tree_sitter_member_names",
        context=context,
    )
    if names != tree_names:
        raise ClangTypeShapeOverlayError(
            f"{context} clang/tree-sitter member names disagree inside a "
            "matched shape"
        )
    _validate_tree_members(
        row.get("tree_sitter_members"), names, context=context
    )
    evidence_json = _member_evidence(
        row.get("clang_members"), names, context=context
    )

    matched_span = _non_empty_str(
        row.get("matched_site_span"), field="matched_site_span", context=context
    )
    matched_line = _coordinate(
        row.get("matched_site_line"), field="matched_site_line", context=context
    )
    matched_col0 = _coordinate(
        row.get("matched_site_col0"), field="matched_site_col0", context=context
    )
    location_origin = _non_empty_str(
        row.get("location_origin"), field="location_origin", context=context
    )

    entry_indices = _row_entry_indices(
        row.get("entry_indices"),
        context=context,
        n_compile_entries=provenance.n_compile_entries,
    )
    compilers_json = _row_compilers_json(
        row.get("compilers"),
        context=context,
        expected_digest=provenance.digest,
        allowed_compilers=provenance.allowed_compilers,
    )
    compilers = json.loads(compilers_json)
    row_compilers = {
        (c["compiler_path"], c["compiler_id"]) for c in compilers
    }
    if row_compilers != {
        provenance.entry_compilers[index] for index in entry_indices
    }:
        raise ClangTypeShapeOverlayError(
            f"{context} compiler provenance disagrees with its compile entries"
        )

    compiler_path = row.get("compiler_path")
    compiler_id = row.get("compiler_id")
    if len(compilers) == 1:
        only = compilers[0]
        compiler_path = _non_empty_str(
            compiler_path, field="compiler_path", context=context
        )
        compiler_id = _non_empty_str(
            compiler_id, field="compiler_id", context=context
        )
        if (compiler_path, compiler_id) != (
            only["compiler_path"],
            only["compiler_id"],
        ):
            raise ClangTypeShapeOverlayError(
                f"{context} singular compiler disagrees with compilers list"
            )
    elif compiler_path is not None or compiler_id is not None:
        raise ClangTypeShapeOverlayError(
            f"{context} exposes singular compiler fields for multiple compilers"
        )
    else:
        compiler_path = None
        compiler_id = None

    if row.get("compile_commands_digest") != provenance.digest:
        raise ClangTypeShapeOverlayError(
            f"{context} compile_commands_digest disagrees with the audit report"
        )

    graph_span = _non_empty_str(
        owner.get("graph_canonical_span"),
        field="graph_canonical_span",
        context=f"type-declaration owner {title!r}",
    )
    matched_is_canonical = owner.get("matched_site_is_canonical")
    if not isinstance(matched_is_canonical, bool):
        raise ClangTypeShapeOverlayError(
            f"type-declaration owner {title!r} has a non-boolean "
            "matched_site_is_canonical"
        )

    desc = (
        f"configured Clang type shape for {title} (kind={kind}; "
        f"fact_kind={FACT_KIND}; ordered direct member names only; "
        f"members={len(names)}; graph_canonical={graph_span}; "
        f"matched_site={matched_span}; member type spellings, enum values, "
        f"bit-field widths and locations are diagnostic evidence, not ABI or "
        f"layout equality; deterministic only relative to recorded Clang + "
        f"compile_commands.json)"
    )
    return {
        "clang_shape_members_validated": True,
        "clang_shape_fact_kind": FACT_KIND,
        "clang_shape_extractor": EXTRACTOR,
        "clang_shape_entity_kind": str(kind),
        "clang_shape_member_count": len(names),
        "clang_shape_member_names": _canonical_json(names),
        "clang_shape_member_evidence": evidence_json,
        "clang_shape_graph_canonical_span": graph_span,
        "clang_shape_matched_site_span": matched_span,
        "clang_shape_matched_site_line": matched_line,
        "clang_shape_matched_site_col0": matched_col0,
        "clang_shape_matched_site_is_canonical": matched_is_canonical,
        "clang_shape_location_origin": location_origin,
        "clang_shape_entry_indices": entry_indices,
        "clang_shape_compiler_path": compiler_path,
        "clang_shape_compiler_id": compiler_id,
        "clang_shape_compilers": compilers_json,
        "clang_shape_compile_commands_digest": provenance.digest,
        "clang_shape_confidence": 1.0,
        "clang_shape_is_deterministic": True,
        "clang_shape_description": desc,
    }


def _validate_payload_against_entity(
    entity: Dict[str, Any], payload: Dict[str, Any], *, title: str
) -> None:
    for key, new_val in payload.items():
        if key in entity and not _values_compatible(entity.get(key), new_val):
            raise ClangTypeShapeOverlayError(
                f"conflicting pre-existing shape field {key!r} on entity "
                f"{title!r}: existing={entity.get(key)!r} new={new_val!r}"
            )


def _apply_payload_to_entity(
    entity: Dict[str, Any], payload: Dict[str, Any], *, title: str
) -> bool:
    _validate_payload_against_entity(entity, payload, title=title)
    changed = False
    for key, new_val in payload.items():
        if key not in entity or entity.get(key) != new_val:
            entity[key] = new_val
            changed = True
    return changed


def _index_shape_entities(
    data: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Map struct/enum entity title -> list of entity dicts (expect length 1)."""
    by_title: Dict[str, List[Dict[str, Any]]] = {}
    for e in data.get("entities") or []:
        if str(e.get("type")) not in SHAPE_ENTITY_KINDS:
            continue
        title = str(e.get("title") or "")
        if not title:
            continue
        by_title.setdefault(title, []).append(e)
    return by_title


def apply_clang_type_shapes_from_reports(
    data: Dict[str, List[Dict[str, Any]]],
    report: Dict[str, Any],
    type_report: Dict[str, Any],
    package_dir: Path,
) -> Dict[str, Any]:
    """Apply precomputed shape + type-declaration reports onto entities.

    Pure relative to both reports: never invokes the compiler. Plans fully
    before mutating. Returns the manifest provenance block.
    """
    package_dir = Path(package_dir).resolve()
    provenance = _validate_report_provenance(
        report,
        package_dir,
        context="type-shape audit report",
        expected_mode=SHAPE_AUDIT_MODE,
    )
    type_provenance = _validate_report_provenance(
        type_report,
        package_dir,
        context="type-declaration audit report",
        expected_mode=TYPE_AUDIT_MODE,
    )
    if provenance.identity() != type_provenance.identity():
        raise ClangTypeShapeOverlayError(
            "type-shape and type-declaration reports disagree on capture "
            "digest, compile entries, or toolchain identity"
        )
    if str(report.get("type_declaration_audit_mode") or "") != TYPE_AUDIT_MODE:
        raise ClangTypeShapeOverlayError(
            "type-shape audit report does not record the type-declaration "
            "audit mode it was derived from"
        )

    buckets, counts = _validated_bucket_counts(report)
    _fail_closed_residuals(buckets)

    owners = _index_shape_owners(type_report)
    if counts["type_declaration_matched_struct_enum"] != len(owners):
        raise ClangTypeShapeOverlayError(
            "type-shape audit struct/enum owner census "
            f"{counts['type_declaration_matched_struct_enum']} disagrees with "
            f"{len(owners)} matched struct/enum type declarations"
        )

    by_title = _index_shape_entities(data)
    matched_sorted = sorted(
        buckets["matched_shape"],
        key=lambda r: (
            str(r.get("source_path") or ""),
            str(r.get("tree_sitter_title") or ""),
            str(r.get("entity_kind") or ""),
            str(r.get("name") or ""),
            int(r.get("matched_site_line") or 0),
            int(r.get("matched_site_col0") or 0),
        ),
    )

    plans: List[Tuple[Dict[str, Any], Dict[str, Any], str, Dict[str, Any]]] = []
    matched_titles: Set[str] = set()
    for row in matched_sorted:
        raw_title = row.get("tree_sitter_title")
        if not isinstance(raw_title, str) or not raw_title:
            raise ClangTypeShapeOverlayError(
                f"matched_shape row missing tree_sitter_title: {row!r}"
            )
        title = raw_title
        if title in matched_titles:
            raise ClangTypeShapeOverlayError(
                f"type-shape audit report contains duplicate matched title "
                f"{title!r}"
            )
        matched_titles.add(title)
        context = f"matched_shape row {title!r}"

        name = row.get("name")
        if (
            not isinstance(name, str)
            or not name
            or title.rsplit(":", 1)[-1] != name
        ):
            raise ClangTypeShapeOverlayError(
                f"matched_shape row title/name mismatch: title={title!r} "
                f"name={name!r}"
            )
        kind = row.get("entity_kind")
        if kind not in SHAPE_ENTITY_KINDS:
            raise ClangTypeShapeOverlayError(
                f"{context} has invalid entity_kind {kind!r}"
            )

        rel_path = row.get("source_path")
        if not isinstance(rel_path, str) or not rel_path:
            raise ClangTypeShapeOverlayError(f"{context} missing source_path")
        try:
            normalized_rel = package_relative_posix(
                (package_dir / Path(rel_path)).resolve(), package_dir
            )
        except (OSError, ValueError) as e:
            raise ClangTypeShapeOverlayError(
                f"{context} has invalid package-relative source_path "
                f"{rel_path!r}: {e}"
            ) from e
        if normalized_rel != rel_path:
            raise ClangTypeShapeOverlayError(
                f"{context} has non-canonical source_path {rel_path!r}; "
                f"expected {normalized_rel!r}"
            )

        # The shape row must describe exactly the owner the declaration audit
        # matched; divergence aborts instead of guessing an attachment site.
        owner = owners.get(title)
        if owner is None:
            raise ClangTypeShapeOverlayError(
                f"{context} has no matched type-declaration owner"
            )
        if owner.get("line_column_confirmed") is not True:
            raise ClangTypeShapeOverlayError(
                f"type-declaration owner {title!r} lacks "
                "line_column_confirmed=true"
            )
        for field in ("entity_kind", "name", "source_path"):
            if owner.get(field) != row.get(field):
                raise ClangTypeShapeOverlayError(
                    f"{context} {field} disagrees with its type-declaration "
                    f"owner: audit={owner.get(field)!r} shape={row.get(field)!r}"
                )
        for field in (
            "matched_site_span",
            "matched_site_line",
            "matched_site_col0",
        ):
            if owner.get(field) != row.get(field):
                raise ClangTypeShapeOverlayError(
                    f"{context} {field} disagrees with its type-declaration "
                    f"owner: audit={owner.get(field)!r} shape={row.get(field)!r}"
                )

        ents = by_title.get(title) or []
        if len(ents) == 0:
            raise ClangTypeShapeOverlayError(
                f"no struct/enum entity for matched_shape title {title!r}"
            )
        if len(ents) > 1:
            raise ClangTypeShapeOverlayError(
                f"non-unique struct/enum entity title {title!r} "
                f"({len(ents)} rows); refuse ambiguous attachment"
            )
        entity = ents[0]
        ent_type = str(entity.get("type") or "")
        if ent_type != kind:
            raise ClangTypeShapeOverlayError(
                f"entity type mismatch for {title!r}: graph={ent_type!r} "
                f"audit={kind!r}"
            )
        symbol_name = entity.get("symbol_name")
        if not isinstance(symbol_name, str) or symbol_name != name:
            raise ClangTypeShapeOverlayError(
                f"symbol_name mismatch for {title!r}: graph={symbol_name!r} "
                f"audit={name!r}"
            )
        ent_rel = _entity_rel_source(entity, package_dir)
        if ent_rel != rel_path:
            raise ClangTypeShapeOverlayError(
                f"source-path mismatch for {title!r}: graph={ent_rel!r} "
                f"audit={rel_path!r}"
            )
        graph_span = str(entity.get("span") or "")
        canonical_span = str(owner.get("graph_canonical_span") or "")
        if not graph_span or graph_span != canonical_span:
            raise ClangTypeShapeOverlayError(
                f"canonical-span mismatch for {title!r}: graph={graph_span!r} "
                f"audit={canonical_span!r}"
            )

        base_snapshot = {
            "id": entity.get("id"),
            "title": entity.get("title"),
            "type": entity.get("type"),
            "source_file": entity.get("source_file"),
            "span": entity.get("span"),
            "extractor": entity.get("extractor"),
            "confidence": entity.get("confidence"),
            "is_deterministic": entity.get("is_deterministic"),
            "text_unit_ids": entity.get("text_unit_ids"),
            "symbol_name": entity.get("symbol_name"),
        }
        payload = _shape_payload(row, owner, provenance=provenance)
        unknown = sorted(
            str(k)
            for k, value in entity.items()
            if str(k).startswith("clang_shape_")
            and str(k) not in _SHAPE_FIELDS
            and not _is_missing(value)
        )
        if unknown:
            raise ClangTypeShapeOverlayError(
                f"unknown pre-existing clang_shape_* fields on {title!r}: "
                f"{unknown}"
            )
        _validate_payload_against_entity(entity, payload, title=title)
        plans.append((entity, payload, title, base_snapshot))

    # Stale clang_shape_* on unmatched or non-shape entities fails closed.
    for entity in data.get("entities") or []:
        material_fields = [
            key
            for key in entity
            if str(key).startswith("clang_shape_")
            and not _is_missing(entity.get(key))
        ]
        if not material_fields:
            continue
        title = str(entity.get("title") or "")
        if str(entity.get("type")) not in SHAPE_ENTITY_KINDS:
            raise ClangTypeShapeOverlayError(
                f"non-struct/enum entity {title!r} carries Clang shape fields: "
                f"{sorted(material_fields)}"
            )
        if title not in matched_titles:
            raise ClangTypeShapeOverlayError(
                f"stale Clang shape fields on unmatched entity {title!r}: "
                f"{sorted(material_fields)}"
            )

    # Mutation begins only after every row/entity/path/provenance check.
    n_changed = 0
    for entity, payload, title, base in plans:
        if _apply_payload_to_entity(entity, payload, title=title):
            n_changed += 1
        for key, expected in base.items():
            if entity.get(key) != expected:
                raise ClangTypeShapeOverlayError(
                    f"internal error: base field {key!r} mutated on {title!r}"
                )

    return {
        "mode": MODE,
        "enabled": True,
        "fact_kind": FACT_KIND,
        "extractor": EXTRACTOR,
        "n_facts": len(plans),
        "n_facts_changed": n_changed,
        "n_decorated_entities": len(plans),
        "n_compile_entries": provenance.n_compile_entries,
        "n_translation_units": provenance.n_translation_units,
        "compiler_path": report.get("compiler_path"),
        "compiler_id": report.get("compiler_id"),
        "compiler_version": report.get("compiler_version"),
        "compilers": provenance.normalized_compilers,
        "compile_commands_digest": provenance.digest,
        "counts": {
            key: counts[key] for key in (*_ALL_REPORT_BUCKETS, *_DERIVED_COUNTS)
        },
        "observation_only_buckets": list(_OBSERVATION_ONLY_BUCKETS),
        "fail_closed_buckets": list(_FAIL_CLOSED_BUCKETS),
        "hard_equality": HARD_EQUALITY,
        "evidence_only": list(EVIDENCE_ONLY),
        "limitations": list(LIMITATIONS),
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }


def append_clang_type_shapes(
    data: Dict[str, List[Dict[str, Any]]],
    package_dir: Path,
    *,
    timeout: int = 120,
    report: Optional[Dict[str, Any]] = None,
    type_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Attach configured type-shape evidence to struct/enum entities.

    Mutates ``data['entities']`` in place. Returns the manifest provenance
    block for ``extra_manifest['clang_type_shapes']``. Pass ``report`` and
    ``type_report`` together (both built from one capture) so index_c can share
    a single AST capture with the other Clang overlays; passing only one is
    refused because the pair must describe the same capture.
    """
    package_dir = Path(package_dir).resolve()
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout <= 0
    ):
        raise ClangTypeShapeOverlayError(
            f"timeout must be a positive integer, got {timeout!r}"
        )
    if (report is None) != (type_report is None):
        raise ClangTypeShapeOverlayError(
            "report and type_report must be provided together or not at all"
        )
    if report is None or type_report is None:
        from c_clang_ast_capture import (  # type: ignore
            ClangAstCaptureError,
            capture_clang_ast_package,
        )
        from c_clang_type_audit import (  # type: ignore
            ClangTypeAuditError,
            build_type_declaration_audit_from_capture,
        )
        from c_clang_type_shape_audit import (  # type: ignore
            build_type_shape_audit_from_capture,
        )

        try:
            capture = capture_clang_ast_package(package_dir, timeout=timeout)
            type_report = build_type_declaration_audit_from_capture(capture)
            report = build_type_shape_audit_from_capture(
                capture, type_report=type_report
            )
        except (
            ClangAstCaptureError,
            ClangTypeAuditError,
            ClangTypeShapeAuditError,
        ) as e:
            raise ClangTypeShapeOverlayError(str(e)) from e
    return apply_clang_type_shapes_from_reports(
        data, report, type_report, package_dir
    )


# ---------------------------------------------------------------------------
# Persisted-overlay integrity contract (read-only; no Clang, no reindex)
# ---------------------------------------------------------------------------

_MAX_ANOMALY_SAMPLES = 40
_MAX_ANOMALY_MESSAGE = 400

ANOMALY_CODES = frozenset(
    {
        "empty_entity_id",
        "duplicate_entity_id",
        "legacy_block_missing_with_fields",
        "off_with_decorated_entities",
        "invalid_enabled_block",
        "stale_shape_metadata",
        "partial_shape_payload",
        "unknown_shape_field",
        "shape_field_type",
        "identity_mismatch",
        "canonical_span_mismatch",
        "member_names_json",
        "member_evidence_json",
        "member_census",
        "member_evidence",
        "entry_index_census",
        "compiler_mismatch",
        "digest_mismatch",
        "forbidden_claim",
        "confidence_boundary",
        "manifest_mode_mismatch",
        "manifest_identity_mismatch",
        "manifest_count_mismatch",
        "manifest_contract_claim",
        "residual_bucket_nonzero",
    }
)

# Member ``form`` values that mark a residual, never a published member.
_RESIDUAL_MEMBER_FORMS = frozenset(
    {"unsupported", "anonymous", "unnamed_bitfield"}
)

# Affirmative proof claims that must never appear in persisted shape evidence.
# The honest producer text only ever *denies* these.
_FORBIDDEN_CLAIM_PATTERNS = (
    re.compile(r"\bABI[- ](?:proof|proven|guarantee[ds]?|compatib\w*)\b", re.I),
    re.compile(r"\blayout[- ](?:proof|proven|guarantee[ds]?|compatib\w*)\b", re.I),
    re.compile(r"\bFFI[- ]safe\b", re.I),
    re.compile(r"\brepr[- ](?:proof|proven|guarantee[ds]?|compatib\w*)\b", re.I),
    re.compile(
        r"\b(?:proves|proven|guarantee[ds]?|verifie[sd])\s+"
        r"(?:the\s+)?(?:ABI|layout|FFI|repr|representation)\b",
        re.I,
    ),
    re.compile(r"\bmulti-config\s+(?:proof|coverage\s+guaranteed)\b", re.I),
    re.compile(r"\bC\+\+\s+(?:support|coverage)\s+(?:proven|guaranteed)\b", re.I),
)


def is_material_value(value: Any) -> bool:
    """True when a parquet/JSON cell holds a real value (not null/NaN/NA)."""
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    try:
        import pandas as pd  # local: optional for pure-dict unit tests

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
    """True for a nested NaN/Infinity without coercing the value."""
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
    """Canonical JSON that refuses NaN/Infinity (audit re-encode contract)."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _scalar(value: Any) -> Any:
    """Unwrap numpy scalars without coercing Python values."""
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _as_int(value: Any) -> Optional[int]:
    """Integer view of a cell; parquet widens ints with nulls to float64."""
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
    """List view of a parquet ndarray/list cell; never invents content."""
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
    entity_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if code not in ANOMALY_CODES:
        raise AssertionError(f"unknown type-shape integrity anomaly code {code!r}")
    row: Dict[str, Any] = {"code": code, "message": _clip(message)}
    if entity_id is not None:
        row["entity_id"] = entity_id
    if extra:
        for key, value in sorted(extra.items()):
            row[key] = _clip(value) if isinstance(value, str) else value
    return row


def _forbidden_claims(text: Any) -> List[str]:
    """Affirmative ABI/layout/FFI/repr proof claims found in evidence text."""
    if not isinstance(text, str):
        return []
    return sorted(
        {
            pattern.search(text).group(0)  # type: ignore[union-attr]
            for pattern in _FORBIDDEN_CLAIM_PATTERNS
            if pattern.search(text)
        }
    )


def _has_material_shape_fields(entity: Any) -> bool:
    for key in _row_keys(entity):
        if key.startswith("clang_shape_") and is_material_value(
            _row_get(entity, key)
        ):
            return True
    return False


def _decoded_json_field(
    raw: Any,
    *,
    entity_id: Optional[str],
    field: str,
    code: str,
    anomalies: List[Dict[str, Any]],
) -> Any:
    """Strictly decode one persisted canonical-JSON string field."""
    if not isinstance(raw, str) or not raw:
        anomalies.append(
            _anomaly(
                code,
                f"{field} is not a non-empty JSON string: {type(raw).__name__}",
                entity_id=entity_id,
            )
        )
        return None
    try:
        decoded = strict_json_loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        anomalies.append(
            _anomaly(
                code, f"{field} is not strict JSON: {error}", entity_id=entity_id
            )
        )
        return None
    if _contains_non_finite(decoded):
        anomalies.append(
            _anomaly(
                code,
                f"{field} contains NaN/Infinity",
                entity_id=entity_id,
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
                entity_id=entity_id,
            )
        )
        return None
    if canonical != raw:
        anomalies.append(
            _anomaly(
                code,
                f"{field} is not producer-canonical JSON",
                entity_id=entity_id,
            )
        )
        return None
    return decoded


def _validate_member_evidence(
    decoded: Any,
    names: List[str],
    *,
    entity_id: Optional[str],
    anomalies: List[Dict[str, Any]],
) -> None:
    """Ordered, residual-free, evidence-only member records."""
    if not isinstance(decoded, list) or not all(
        isinstance(member, dict) for member in decoded
    ):
        anomalies.append(
            _anomaly(
                "member_evidence",
                "clang_shape_member_evidence must be a list of objects",
                entity_id=entity_id,
            )
        )
        return
    if len(decoded) != len(names):
        anomalies.append(
            _anomaly(
                "member_census",
                f"member evidence rows {len(decoded)} != member names "
                f"{len(names)}",
                entity_id=entity_id,
            )
        )
        return
    expected_keys = set(_MEMBER_EVIDENCE_KEYS)
    for position, member in enumerate(decoded):
        if set(member) != expected_keys:
            anomalies.append(
                _anomaly(
                    "member_evidence",
                    f"member {position} evidence keys "
                    f"{sorted(member)} != {sorted(expected_keys)}",
                    entity_id=entity_id,
                )
            )
            continue
        if member.get("order") != position:
            anomalies.append(
                _anomaly(
                    "member_evidence",
                    f"member {position} has order {member.get('order')!r}",
                    entity_id=entity_id,
                )
            )
        if member.get("name") != names[position]:
            anomalies.append(
                _anomaly(
                    "member_evidence",
                    f"member {position} name {member.get('name')!r} disagrees "
                    f"with published name {names[position]!r}",
                    entity_id=entity_id,
                )
            )
        form = member.get("form")
        if not isinstance(form, str) or not form:
            anomalies.append(
                _anomaly(
                    "member_evidence",
                    f"member {position} has empty form",
                    entity_id=entity_id,
                )
            )
        elif form in _RESIDUAL_MEMBER_FORMS:
            anomalies.append(
                _anomaly(
                    "member_evidence",
                    f"member {position} is a residual form {form!r}",
                    entity_id=entity_id,
                )
            )
        if not isinstance(member.get("is_bitfield"), bool):
            anomalies.append(
                _anomaly(
                    "member_evidence",
                    f"member {position} is_bitfield is not boolean",
                    entity_id=entity_id,
                )
            )
        for field in ("bit_width", "enum_value", "line", "col0"):
            value = member.get(field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                anomalies.append(
                    _anomaly(
                        "member_evidence",
                        f"member {position} {field}={value!r} is not an "
                        "integer or null",
                        entity_id=entity_id,
                    )
                )
            elif field in {"bit_width", "col0"} and value < 0:
                anomalies.append(
                    _anomaly(
                        "member_evidence",
                        f"member {position} {field}={value!r} is negative",
                        entity_id=entity_id,
                    )
                )
            elif field == "line" and value < 1:
                anomalies.append(
                    _anomaly(
                        "member_evidence",
                        f"member {position} line={value!r} is not positive",
                        entity_id=entity_id,
                    )
                )
        for field in ("qualType", "desugaredQualType"):
            value = member.get(field)
            if value is not None and not isinstance(value, str):
                anomalies.append(
                    _anomaly(
                        "member_evidence",
                        f"member {position} {field} is not a string or null",
                        entity_id=entity_id,
                    )
                )


def _validate_decorated_entity(
    entity: Any,
    *,
    entity_id: Optional[str],
    manifest_digest: Optional[str],
    manifest_compilers: Set[Tuple[str, str]],
    n_compile_entries: Optional[int],
    anomalies: List[Dict[str, Any]],
) -> int:
    """Validate one decorated entity; return its decoded member count."""
    present = {
        key
        for key in _row_keys(entity)
        if key.startswith("clang_shape_")
        and is_material_value(_row_get(entity, key))
    }
    unknown = sorted(present - set(_SHAPE_FIELDS))
    if unknown:
        anomalies.append(
            _anomaly(
                "unknown_shape_field",
                f"unknown clang_shape_* fields: {unknown}",
                entity_id=entity_id,
            )
        )
    # compiler_path/compiler_id are legitimately null for multi-compiler rows.
    optional_null = {"clang_shape_compiler_path", "clang_shape_compiler_id"}
    missing = sorted(
        field
        for field in _SHAPE_FIELDS
        if field not in present and field not in optional_null
    )
    if missing:
        anomalies.append(
            _anomaly(
                "partial_shape_payload",
                f"decorated entity is missing required fields: {missing}",
                entity_id=entity_id,
            )
        )

    if _as_bool(_row_get(entity, "clang_shape_members_validated")) is not True:
        anomalies.append(
            _anomaly(
                "shape_field_type",
                "clang_shape_members_validated is not boolean true",
                entity_id=entity_id,
            )
        )
    if _row_get(entity, "clang_shape_fact_kind") != FACT_KIND:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_shape_fact_kind="
                f"{_row_get(entity, 'clang_shape_fact_kind')!r} expected "
                f"{FACT_KIND!r}",
                entity_id=entity_id,
            )
        )
    if _row_get(entity, "clang_shape_extractor") != EXTRACTOR:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_shape_extractor="
                f"{_row_get(entity, 'clang_shape_extractor')!r} expected "
                f"{EXTRACTOR!r}",
                entity_id=entity_id,
            )
        )
    entity_type = str(_row_get(entity, "type") or "")
    shape_kind = _row_get(entity, "clang_shape_entity_kind")
    if shape_kind != entity_type:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_shape_entity_kind={shape_kind!r} != entity type "
                f"{entity_type!r}",
                entity_id=entity_id,
            )
        )
    graph_span = _row_get(entity, "clang_shape_graph_canonical_span")
    entity_span = _row_get(entity, "span")
    if (
        not isinstance(graph_span, str)
        or not graph_span
        or not isinstance(entity_span, str)
        or graph_span != entity_span
    ):
        anomalies.append(
            _anomaly(
                "canonical_span_mismatch",
                f"clang_shape_graph_canonical_span={graph_span!r} != entity "
                f"span {entity_span!r}",
                entity_id=entity_id,
            )
        )
    if not _is_one(_row_get(entity, "clang_shape_confidence")):
        anomalies.append(
            _anomaly(
                "shape_field_type",
                f"clang_shape_confidence="
                f"{_row_get(entity, 'clang_shape_confidence')!r} expected 1.0",
                entity_id=entity_id,
            )
        )
    if _as_bool(_row_get(entity, "clang_shape_is_deterministic")) is not True:
        anomalies.append(
            _anomaly(
                "shape_field_type",
                "clang_shape_is_deterministic is not boolean true",
                entity_id=entity_id,
            )
        )
    if _as_bool(_row_get(entity, "clang_shape_matched_site_is_canonical")) is None:
        anomalies.append(
            _anomaly(
                "shape_field_type",
                "clang_shape_matched_site_is_canonical is not boolean",
                entity_id=entity_id,
            )
        )
    for field, minimum in (
        ("clang_shape_matched_site_line", 1),
        ("clang_shape_matched_site_col0", 0),
    ):
        value = _as_int(_row_get(entity, field))
        if value is None or value < minimum:
            anomalies.append(
                _anomaly(
                    "shape_field_type",
                    f"{field}={_row_get(entity, field)!r} is not an integer "
                    f">= {minimum}",
                    entity_id=entity_id,
                )
            )
    for field in ("clang_shape_matched_site_span", "clang_shape_location_origin"):
        value = _row_get(entity, field)
        if not isinstance(value, str) or not value.strip():
            anomalies.append(
                _anomaly(
                    "shape_field_type",
                    f"{field} is not a non-empty string",
                    entity_id=entity_id,
                )
            )

    # Member census: names and evidence are canonical deterministic JSON.
    member_count = _as_int(_row_get(entity, "clang_shape_member_count"))
    if member_count is None or member_count < 0:
        anomalies.append(
            _anomaly(
                "shape_field_type",
                f"clang_shape_member_count="
                f"{_row_get(entity, 'clang_shape_member_count')!r} is not a "
                "non-negative integer",
                entity_id=entity_id,
            )
        )
    names_decoded = _decoded_json_field(
        _row_get(entity, "clang_shape_member_names"),
        entity_id=entity_id,
        field="clang_shape_member_names",
        code="member_names_json",
        anomalies=anomalies,
    )
    names: List[str] = []
    if names_decoded is not None:
        if not isinstance(names_decoded, list) or not all(
            isinstance(name, str) and name for name in names_decoded
        ):
            anomalies.append(
                _anomaly(
                    "member_census",
                    "clang_shape_member_names must be a list of non-empty "
                    "strings",
                    entity_id=entity_id,
                )
            )
        elif len(set(names_decoded)) != len(names_decoded):
            anomalies.append(
                _anomaly(
                    "member_census",
                    "clang_shape_member_names contains duplicate names",
                    entity_id=entity_id,
                )
            )
        else:
            names = list(names_decoded)
            if member_count is not None and member_count != len(names):
                anomalies.append(
                    _anomaly(
                        "member_census",
                        f"clang_shape_member_count={member_count} != "
                        f"{len(names)} published member names",
                        entity_id=entity_id,
                    )
                )
    evidence_decoded = _decoded_json_field(
        _row_get(entity, "clang_shape_member_evidence"),
        entity_id=entity_id,
        field="clang_shape_member_evidence",
        code="member_evidence_json",
        anomalies=anomalies,
    )
    if evidence_decoded is not None and names_decoded is not None:
        _validate_member_evidence(
            evidence_decoded, names, entity_id=entity_id, anomalies=anomalies
        )

    # Compile-entry census.
    entry_indices = _normalize_list_field(
        _row_get(entity, "clang_shape_entry_indices")
    )
    if entry_indices is None or not entry_indices:
        anomalies.append(
            _anomaly(
                "entry_index_census",
                "clang_shape_entry_indices is not a non-empty list",
                entity_id=entity_id,
            )
        )
    else:
        decoded_indices = [_as_int(index) for index in entry_indices]
        if any(index is None or index < 0 for index in decoded_indices):
            anomalies.append(
                _anomaly(
                    "entry_index_census",
                    f"clang_shape_entry_indices has non-integer entries: "
                    f"{entry_indices!r}",
                    entity_id=entity_id,
                )
            )
        else:
            values = [int(index) for index in decoded_indices]  # type: ignore[arg-type]
            if values != sorted(set(values)):
                anomalies.append(
                    _anomaly(
                        "entry_index_census",
                        f"clang_shape_entry_indices is not sorted/unique: "
                        f"{values!r}",
                        entity_id=entity_id,
                    )
                )
            if n_compile_entries is not None and any(
                index >= n_compile_entries for index in values
            ):
                anomalies.append(
                    _anomaly(
                        "entry_index_census",
                        f"clang_shape_entry_indices {values!r} outside manifest "
                        f"compile-entry census (n={n_compile_entries})",
                        entity_id=entity_id,
                    )
                )

    # Compiler + digest provenance.
    digest = _row_get(entity, "clang_shape_compile_commands_digest")
    if not isinstance(digest, str) or not digest.strip():
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"clang_shape_compile_commands_digest is empty: {digest!r}",
                entity_id=entity_id,
            )
        )
        digest = None
    elif manifest_digest is not None and digest.strip() != manifest_digest:
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"entity digest {digest.strip()!r} != manifest "
                f"{manifest_digest!r}",
                entity_id=entity_id,
            )
        )
    compilers_decoded = _decoded_json_field(
        _row_get(entity, "clang_shape_compilers"),
        entity_id=entity_id,
        field="clang_shape_compilers",
        code="compiler_mismatch",
        anomalies=anomalies,
    )
    identities: List[Tuple[str, str]] = []
    if compilers_decoded is not None:
        if (
            not isinstance(compilers_decoded, list)
            or not compilers_decoded
            or not all(isinstance(row, dict) for row in compilers_decoded)
        ):
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    "clang_shape_compilers must be a non-empty list of objects",
                    entity_id=entity_id,
                )
            )
        else:
            expected_keys = {
                "compiler_path",
                "compiler_id",
                "compile_commands_digest",
            }
            for position, compiler in enumerate(compilers_decoded):
                if set(compiler) != expected_keys:
                    anomalies.append(
                        _anomaly(
                            "compiler_mismatch",
                            f"clang_shape_compilers[{position}] keys "
                            f"{sorted(compiler)} != {sorted(expected_keys)}",
                            entity_id=entity_id,
                        )
                    )
                    continue
                path = compiler.get("compiler_path")
                cid = compiler.get("compiler_id")
                if (
                    not isinstance(path, str)
                    or not path.strip()
                    or not isinstance(cid, str)
                    or not cid.strip()
                ):
                    anomalies.append(
                        _anomaly(
                            "compiler_mismatch",
                            f"clang_shape_compilers[{position}] has incomplete "
                            "identity",
                            entity_id=entity_id,
                        )
                    )
                    continue
                if digest is not None and compiler.get(
                    "compile_commands_digest"
                ) != digest.strip():
                    anomalies.append(
                        _anomaly(
                            "digest_mismatch",
                            f"clang_shape_compilers[{position}] digest disagrees "
                            "with the entity digest",
                            entity_id=entity_id,
                        )
                    )
                identity = (path.strip(), cid.strip())
                if identity in identities:
                    anomalies.append(
                        _anomaly(
                            "compiler_mismatch",
                            f"clang_shape_compilers contains duplicate "
                            f"{identity!r}",
                            entity_id=entity_id,
                        )
                    )
                    continue
                identities.append(identity)
            if manifest_compilers:
                outside = sorted(
                    set(identities) - manifest_compilers
                )
                if outside:
                    anomalies.append(
                        _anomaly(
                            "compiler_mismatch",
                            f"entity compilers absent from manifest census: "
                            f"{outside!r}",
                            entity_id=entity_id,
                        )
                    )

    singular_path = _row_get(entity, "clang_shape_compiler_path")
    singular_id = _row_get(entity, "clang_shape_compiler_id")
    has_singular = is_material_value(singular_path) or is_material_value(
        singular_id
    )
    if len(identities) == 1:
        if (
            not isinstance(singular_path, str)
            or not isinstance(singular_id, str)
            or (singular_path.strip(), singular_id.strip()) != identities[0]
        ):
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    "singular compiler fields disagree with "
                    "clang_shape_compilers",
                    entity_id=entity_id,
                )
            )
    elif len(identities) > 1 and has_singular:
        anomalies.append(
            _anomaly(
                "compiler_mismatch",
                "multi-compiler entity exposes singular compiler fields",
                entity_id=entity_id,
            )
        )

    # Evidence boundary text.
    description = _row_get(entity, "clang_shape_description")
    if not isinstance(description, str) or not description.strip():
        anomalies.append(
            _anomaly(
                "confidence_boundary",
                "clang_shape_description is empty",
                entity_id=entity_id,
            )
        )
    else:
        absent = [
            required
            for required in DESCRIPTION_REQUIRED_SUBSTRINGS
            if required not in description
        ]
        if absent:
            anomalies.append(
                _anomaly(
                    "confidence_boundary",
                    f"clang_shape_description drops its evidence boundary: "
                    f"{absent}",
                    entity_id=entity_id,
                )
            )
        claims = _forbidden_claims(description)
        if claims:
            anomalies.append(
                _anomaly(
                    "forbidden_claim",
                    f"clang_shape_description claims {claims}",
                    entity_id=entity_id,
                )
            )
    return len(names)


def _validate_shape_manifest_block(
    block: Dict[str, Any],
    *,
    n_decorated: int,
    n_entities: int,
    manifest_obj: Dict[str, Any],
    anomalies: List[Dict[str, Any]],
) -> Tuple[Optional[str], Set[Tuple[str, str]], Optional[int], Dict[str, int]]:
    """Validate the enabled manifest block; return its census for entities."""
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
    for field, expected in (
        ("fact_kind", FACT_KIND),
        ("extractor", EXTRACTOR),
    ):
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
        for key in (*_ALL_REPORT_BUCKETS, *_DERIVED_COUNTS):
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
        for key in _FAIL_CLOSED_BUCKETS:
            if counts.get(key):
                anomalies.append(
                    _anomaly(
                        "residual_bucket_nonzero",
                        f"fail-closed residual counts.{key}={counts[key]} must "
                        "be zero in a published overlay",
                    )
                )
        if "matched_shape" in counts and counts["matched_shape"] != n_decorated:
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"manifest counts.matched_shape={counts['matched_shape']} "
                    f"!= {n_decorated} decorated entities",
                )
            )
        if {"shape_owners_classified", "matched_shape", "unsupported_member_form"} <= set(
            counts
        ) and not any(counts.get(key) for key in _FAIL_CLOSED_BUCKETS):
            expected_classified = (
                counts["matched_shape"] + counts["unsupported_member_form"]
            )
            if counts["shape_owners_classified"] != expected_classified:
                anomalies.append(
                    _anomaly(
                        "manifest_count_mismatch",
                        f"counts.shape_owners_classified="
                        f"{counts['shape_owners_classified']} != "
                        f"{expected_classified} classified shape owners",
                    )
                )
        if {
            "shape_owners_classified",
            "type_declaration_matched_struct_enum",
        } <= set(counts) and (
            counts["shape_owners_classified"]
            != counts["type_declaration_matched_struct_enum"]
        ):
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    "counts.shape_owners_classified != "
                    "counts.type_declaration_matched_struct_enum",
                )
            )
        if {
            "type_declaration_matched_total",
            "type_declaration_matched_struct_enum",
        } <= set(counts) and (
            counts["type_declaration_matched_total"]
            < counts["type_declaration_matched_struct_enum"]
        ):
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    "counts.type_declaration_matched_total < "
                    "counts.type_declaration_matched_struct_enum",
                )
            )

    for field in ("n_facts", "n_decorated_entities"):
        value = block.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value != n_decorated
        ):
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"manifest {field}={value!r} != {n_decorated} decorated "
                    "entities",
                )
            )
    n_changed = block.get("n_facts_changed")
    if (
        isinstance(n_changed, bool)
        or not isinstance(n_changed, int)
        or n_changed < 0
        or n_changed > n_decorated
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"manifest n_facts_changed={n_changed!r} is not within "
                f"[0, {n_decorated}]",
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

    raw_digest = block.get("compile_commands_digest")
    digest: Optional[str] = None
    if not isinstance(raw_digest, str) or not raw_digest.strip():
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"manifest compile_commands_digest missing: {raw_digest!r}",
            )
        )
    else:
        digest = raw_digest.strip()

    compilers_block = block.get("compilers")
    identities: Set[Tuple[str, str]] = set()
    valid_compilers: List[Dict[str, Any]] = []
    if (
        not isinstance(compilers_block, list)
        or not compilers_block
        or not all(isinstance(row, dict) for row in compilers_block)
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
            path = compiler.get("compiler_path")
            cid = compiler.get("compiler_id")
            if (
                not isinstance(path, str)
                or not path.strip()
                or not Path(path.strip()).is_absolute()
                or not isinstance(cid, str)
                or not cid.strip()
            ):
                anomalies.append(
                    _anomaly(
                        "manifest_identity_mismatch",
                        f"manifest compilers[{position}] has incomplete identity",
                    )
                )
                continue
            identity = (path.strip(), cid.strip())
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
        try:
            encoded = [
                _strict_canonical_json(compiler) for compiler in compilers_block
            ]
            if encoded != sorted(encoded):
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
        if block.get("compiler_version") is not None and block.get(
            "compiler_version"
        ) != only.get("compiler_version"):
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

    # Persisted contract text: exact producer wording, no proof claims.
    for field, expected in (
        ("hard_equality", HARD_EQUALITY),
        ("evidence_only", list(EVIDENCE_ONLY)),
        ("limitations", list(LIMITATIONS)),
        ("observation_only_buckets", list(_OBSERVATION_ONLY_BUCKETS)),
        ("fail_closed_buckets", list(_FAIL_CLOSED_BUCKETS)),
        ("confidence_boundary", CONFIDENCE_BOUNDARY),
    ):
        if block.get(field) != expected:
            anomalies.append(
                _anomaly(
                    "manifest_contract_claim",
                    f"manifest {field} differs from the producer contract",
                )
            )
    claim_text = [block.get("hard_equality"), block.get("confidence_boundary")]
    limitations = block.get("limitations")
    if isinstance(limitations, list):
        claim_text.extend(limitations)
    evidence_only = block.get("evidence_only")
    if isinstance(evidence_only, list):
        claim_text.extend(evidence_only)
    claims = sorted({c for text in claim_text for c in _forbidden_claims(text)})
    if claims:
        anomalies.append(
            _anomaly(
                "forbidden_claim",
                f"manifest block claims {claims}",
            )
        )

    declared = manifest_obj.get("counts")
    declared_entities = (
        declared.get("entities") if isinstance(declared, dict) else None
    )
    if (
        isinstance(declared_entities, bool)
        or not isinstance(declared_entities, int)
        or declared_entities != n_entities
    ):
        anomalies.append(
            _anomaly(
                "manifest_count_mismatch",
                f"manifest counts.entities={declared_entities!r} != entity "
                f"table length {n_entities}",
            )
        )
    return digest, identities, n_compile_entries, counts


def validate_persisted_type_shape_overlay(
    entities: Any,
    manifest: Optional[Any] = None,
    *,
    max_anomaly_samples: int = _MAX_ANOMALY_SAMPLES,
) -> Dict[str, Any]:
    """Validate already-persisted ``clang_shape_*`` entity evidence.

    Pure and non-mutating. Never invokes Clang, loads compile_commands.json,
    reindexes, or rewrites graphs.

    Compatibility states:
      * no ``clang_type_shapes`` block and zero shape fields -> ``legacy_absent``
      * ``mode=off`` / ``enabled=false`` -> requires zero shape fields
      * enabled ``configured_clang_type_shapes`` -> full entity + manifest census

    A missing manifest block never legitimizes existing ``clang_shape_*``
    fields, and nothing is ever repaired.
    """
    if max_anomaly_samples < 0:
        max_anomaly_samples = 0

    entities_list = _table_rows(entities, name="entities")
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
    for index, entity in enumerate(entities_list):
        raw_id = _row_get(entity, "id")
        if not is_material_value(raw_id) or not str(raw_id).strip():
            anomalies.append(
                _anomaly(
                    "empty_entity_id",
                    f"entity at index {index} has empty id",
                )
            )
            continue
        entity_id = str(raw_id)
        if entity_id in seen_ids:
            anomalies.append(
                _anomaly(
                    "duplicate_entity_id",
                    f"duplicate entity id {entity_id!r}",
                    entity_id=entity_id,
                    extra={"other_index": seen_ids[entity_id]},
                )
            )
        else:
            seen_ids[entity_id] = index

    carrying: List[Dict[str, Any]] = []
    decorated: List[Dict[str, Any]] = []
    for entity in entities_list:
        if not _has_material_shape_fields(entity):
            continue
        carrying.append(entity)
        entity_id = str(_row_get(entity, "id") or "") or None
        if str(_row_get(entity, "type") or "") in SHAPE_ENTITY_KINDS:
            decorated.append(entity)
        else:
            anomalies.append(
                _anomaly(
                    "stale_shape_metadata",
                    f"non-struct/enum entity carries clang_shape_* fields "
                    f"(type={_row_get(entity, 'type')!r})",
                    entity_id=entity_id,
                )
            )

    has_block = "clang_type_shapes" in manifest_obj
    block = manifest_obj.get("clang_type_shapes")
    mode_state = "legacy_absent"
    block_enabled = False

    if not has_block:
        if carrying:
            anomalies.append(
                _anomaly(
                    "legacy_block_missing_with_fields",
                    f"manifest lacks clang_type_shapes but graph has "
                    f"{len(carrying)} entity/entities with clang_shape_* fields",
                    extra={"n_entities": len(carrying)},
                )
            )
            mode_state = "invalid"
    elif not isinstance(block, dict):
        anomalies.append(
            _anomaly(
                "invalid_enabled_block",
                f"clang_type_shapes manifest block is not an object: "
                f"{type(block).__name__}",
            )
        )
        mode_state = "invalid"
    else:
        mode = block.get("mode")
        enabled = block.get("enabled")
        if mode == "off" and enabled is False:
            mode_state = "off"
            if carrying:
                anomalies.append(
                    _anomaly(
                        "off_with_decorated_entities",
                        f"clang_type_shapes is off/disabled but graph has "
                        f"{len(carrying)} entity/entities with clang_shape_* "
                        "fields",
                        extra={"n_entities": len(carrying)},
                    )
                )
            disabled_counts = {
                key: block.get(key)
                for key in ("n_facts", "n_translation_units")
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
                    f"clang_type_shapes enablement inconsistent: mode={mode!r} "
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
        ) = _validate_shape_manifest_block(
            block,
            n_decorated=len(decorated),
            n_entities=len(entities_list),
            manifest_obj=manifest_obj,
            anomalies=anomalies,
        )

    n_members = 0
    if block_enabled or (mode_state == "invalid" and carrying):
        for entity in sorted(
            decorated,
            key=lambda e: (
                str(_row_get(e, "title") or ""),
                str(_row_get(e, "id") or ""),
            ),
        ):
            n_members += _validate_decorated_entity(
                entity,
                entity_id=str(_row_get(entity, "id") or "") or None,
                manifest_digest=manifest_digest,
                manifest_compilers=manifest_compilers,
                n_compile_entries=n_compile_entries,
                anomalies=anomalies,
            )

    anomalies.sort(
        key=lambda a: (
            str(a.get("code") or ""),
            str(a.get("entity_id") or ""),
            str(a.get("message") or ""),
            _strict_canonical_json(
                {
                    key: a[key]
                    for key in sorted(a)
                    if key not in {"code", "message", "entity_id"}
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
        "n_entities": len(entities_list),
        "n_decorated_entities": len(decorated),
        "n_shape_field_carriers": len(carrying),
        "n_members_validated": n_members,
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
        "hard_equality": HARD_EQUALITY,
        "limitations": list(LIMITATIONS),
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }
