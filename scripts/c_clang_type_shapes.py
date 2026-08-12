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
        "hard_equality": "ordered direct member names only",
        "evidence_only": [
            "qualType",
            "desugaredQualType",
            "enum_value",
            "bit_width",
            "locations",
        ],
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
