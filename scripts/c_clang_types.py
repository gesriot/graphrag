#!/usr/bin/env python
"""Optional Clang configured type-declaration evidence overlay.

Publishes configuration/toolchain-derived Clang type-declaration fields onto
**existing** tree-sitter-c ``struct`` / ``enum`` / ``typedef`` entities from
``build_type_declaration_audit_from_capture`` matched rows only.

This is **not**:
  * a type graph or ``uses_type`` relationship layer
  * alternate-site entities or span rewrites of the graph representative
  * layout, ABI, type-use, multi-config, or macro-complete proof
  * silent application of residual / unconfirmed / non-unique audit rows

Only matched audit rows with a unique graph entity (exact
``tree_sitter_title``, entity type, ``symbol_name``, package-relative source
path, and canonical graph span) are applied. Validation is atomic: any error
leaves ``data`` unchanged.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from c_clang_type_audit import (  # type: ignore
    ClangTypeAuditError,
    run_clang_type_audit,
)
from c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    build_disabled_overlay_provenance,
)
from c_identities import package_relative_posix  # type: ignore

MODE = "configured_clang_type_declarations"
FACT_KIND = "configured_type_declaration"
EXTRACTOR = "clang-ast-json"
TYPE_ENTITY_KINDS = frozenset({"struct", "enum", "typedef"})
_SPAN_START_RE = re.compile(r"^(?P<line>[1-9][0-9]*):(?P<col>[0-9]+)-")
CONFIDENCE_BOUNDARY = (
    "clang_type_confidence=1.0 and clang_type_is_deterministic=true mean the "
    "type-declaration confirmation is re-derivable from the recorded Clang + "
    "compile_commands.json configuration via the AST type-declaration audit, "
    "not that it is pure syntax, layout/ABI-complete, multi-config, type-use "
    "proof, or free of observation-only residuals. Standard mismatch residuals "
    "(tree_sitter_only, clang_only, ambiguous, macro_location_unsupported) "
    "fail closed. out_of_compile_db_scope, anonymous_declarations, "
    "unsupported_declarations, outside_package_declarations, and "
    "alternate_declaration_sites remain observation-only and receive no "
    "invented type entities or uses_type edges."
)

# Audit-report contract text only; not a persisted manifest field.
LIMITATIONS = (
    "Type-declaration confirmation of existing struct/enum/typedef entities only",
    "Not a type graph or uses_type relationship layer",
    "Not layout or ABI proof",
    "Not type-use proof",
    "Not multi-config coverage",
    "Not macro-complete proof",
    "Observation-only residuals invent no entities or alternate-site entities",
)

# Substrings every published clang_type_description must keep.
DESCRIPTION_REQUIRED_SUBSTRINGS = (
    "configured Clang type declaration for",
    "deterministic only relative to recorded Clang + compile_commands.json",
)

# Fields written onto type entities (scalar / list / JSON text).
# Optional type-string fields may be null when Clang did not supply them.
_TYPE_FIELDS = (
    "clang_type_declaration_confirmed",
    "clang_type_fact_kind",
    "clang_type_extractor",
    "clang_type_entity_kind",
    "clang_type_qual_type",
    "clang_type_desugared_qual_type",
    "clang_type_fixed_underlying_type",
    "clang_type_graph_canonical_span",
    "clang_type_graph_canonical_line",
    "clang_type_graph_canonical_col0",
    "clang_type_matched_site_span",
    "clang_type_matched_site_line",
    "clang_type_matched_site_col0",
    "clang_type_matched_site_is_canonical",
    "clang_type_location_origin",
    "clang_type_entry_indices",
    "clang_type_compiler_path",
    "clang_type_compiler_id",
    "clang_type_compilers",
    "clang_type_compile_commands_digest",
    "clang_type_confidence",
    "clang_type_is_deterministic",
    "clang_type_description",
)

# Fail-closed residual buckets (any nonzero blocks publication).
_FAIL_CLOSED_BUCKETS = (
    "tree_sitter_only",
    "clang_only",
    "ambiguous",
    "macro_location_unsupported",
)

# Observation-only residuals (allowed; never invent entities).
_OBSERVATION_ONLY_BUCKETS = (
    "out_of_compile_db_scope",
    "anonymous_declarations",
    "unsupported_declarations",
    "outside_package_declarations",
    "alternate_declaration_sites",
)

_ALL_REPORT_BUCKETS = (
    "matched",
    *_FAIL_CLOSED_BUCKETS,
    *_OBSERVATION_ONLY_BUCKETS,
)


class ClangTypeOverlayError(CompilerOverlayError):
    """Raised when the type-declaration overlay cannot apply honestly."""


def build_disabled_provenance() -> Dict[str, Any]:
    """Stable manifest block when ``--clang-types`` is off."""
    return build_disabled_overlay_provenance()


def _entity_rel_source(entity: Dict[str, Any], package_dir: Path) -> str:
    package_dir = package_dir.resolve()
    raw = str(entity.get("source_file") or "")
    if not raw:
        raise ClangTypeOverlayError(
            f"entity {entity.get('title')!r} has empty source_file"
        )
    p = Path(raw)
    if not p.is_absolute():
        p = (package_dir / p).resolve()
    else:
        p = p.resolve()
    try:
        return package_relative_posix(p, package_dir)
    except (OSError, ValueError) as e:
        raise ClangTypeOverlayError(
            f"cannot normalize source_file {raw!r} for entity "
            f"{entity.get('title')!r}: {e}"
        ) from e


def _entry_indices(
    value: Any, *, context: str, n_compile_entries: int
) -> List[int]:
    if not isinstance(value, list) or not value:
        raise ClangTypeOverlayError(
            f"{context} must have a non-empty list entry_indices"
        )
    out: Set[int] = set()
    for raw in value:
        if (
            isinstance(raw, bool)
            or not isinstance(raw, int)
            or raw < 0
            or raw >= n_compile_entries
        ):
            raise ClangTypeOverlayError(
                f"{context} has invalid compile entry index {raw!r}"
            )
        if raw in out:
            raise ClangTypeOverlayError(
                f"{context} contains duplicate entry indices"
            )
        out.add(raw)
    if value != sorted(out):
        raise ClangTypeOverlayError(
            f"{context} entry_indices must be sorted and unique"
        )
    return list(value)


def _span_start(value: Any, *, field: str, title: str) -> Tuple[int, int]:
    if not isinstance(value, str):
        raise ClangTypeOverlayError(
            f"matched row {title!r} has non-string {field}"
        )
    match = _SPAN_START_RE.match(value)
    if match is None:
        raise ClangTypeOverlayError(
            f"matched row {title!r} has invalid {field} {value!r}"
        )
    return int(match.group("line")), int(match.group("col"))


def _validated_coordinate(value: Any, *, field: str, title: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClangTypeOverlayError(
            f"matched row {title!r} has invalid {field} {value!r}"
        )
    if field.endswith("line") and value < 1:
        raise ClangTypeOverlayError(
            f"matched row {title!r} has non-positive {field}"
        )
    return value


def _validate_site_coordinates(row: Dict[str, Any], *, title: str) -> None:
    """Reject internally inconsistent exact-site evidence."""
    graph_line = _validated_coordinate(
        row.get("graph_canonical_line"),
        field="graph_canonical_line",
        title=title,
    )
    graph_col = _validated_coordinate(
        row.get("graph_canonical_col0"),
        field="graph_canonical_col0",
        title=title,
    )
    matched_line = _validated_coordinate(
        row.get("matched_site_line"), field="matched_site_line", title=title
    )
    matched_col = _validated_coordinate(
        row.get("matched_site_col0"), field="matched_site_col0", title=title
    )
    graph_span = row.get("graph_canonical_span")
    matched_span = row.get("matched_site_span")
    if _span_start(
        graph_span, field="graph_canonical_span", title=title
    ) != (graph_line, graph_col):
        raise ClangTypeOverlayError(
            f"matched row {title!r} graph canonical span/start disagreement"
        )
    if _span_start(
        matched_span, field="matched_site_span", title=title
    ) != (matched_line, matched_col):
        raise ClangTypeOverlayError(
            f"matched row {title!r} matched-site span/start disagreement"
        )

    tree_line = _validated_coordinate(
        row.get("tree_sitter_line"), field="tree_sitter_line", title=title
    )
    tree_col = _validated_coordinate(
        row.get("tree_sitter_col"), field="tree_sitter_col", title=title
    )
    clang_line = _validated_coordinate(
        row.get("clang_line"), field="clang_line", title=title
    )
    clang_col = _validated_coordinate(
        row.get("clang_col0"), field="clang_col0", title=title
    )
    if (tree_line, tree_col) != (matched_line, matched_col):
        raise ClangTypeOverlayError(
            f"matched row {title!r} tree-sitter coordinates disagree with "
            "matched site"
        )
    if (clang_line, clang_col) != (matched_line, matched_col):
        raise ClangTypeOverlayError(
            f"matched row {title!r} Clang coordinates disagree with matched site"
        )
    clang_col1 = row.get("clang_col1")
    if clang_col1 is not None:
        if (
            isinstance(clang_col1, bool)
            or not isinstance(clang_col1, int)
            or clang_col1 != clang_col + 1
        ):
            raise ClangTypeOverlayError(
                f"matched row {title!r} has inconsistent clang_col1"
            )

    same_site = (
        graph_span == matched_span
        and (graph_line, graph_col) == (matched_line, matched_col)
    )
    graph_is_matched = row.get("graph_canonical_is_matched_site")
    matched_is_canonical = row.get("matched_site_is_canonical")
    if not isinstance(graph_is_matched, bool) or not isinstance(
        matched_is_canonical, bool
    ):
        raise ClangTypeOverlayError(
            f"matched row {title!r} has non-boolean canonical-site markers"
        )
    if graph_is_matched != same_site or matched_is_canonical != same_site:
        raise ClangTypeOverlayError(
            f"matched row {title!r} has inconsistent canonical-site markers"
        )


def _optional_type_string(value: Any, *, field: str, title: str) -> Optional[str]:
    """Normalize optional Clang type-string fields; empty becomes null."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClangTypeOverlayError(
            f"matched row {title!r} has non-string {field}"
        )
    stripped = value.strip()
    return stripped if stripped else None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


def _canonical_compilers_json(
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
        raise ClangTypeOverlayError(
            f"{context} compilers must be a non-empty list of objects"
        )
    normalized: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    for i, compiler in enumerate(value):
        cctx = f"{context}.compilers[{i}]"
        path = compiler.get("compiler_path")
        cid = compiler.get("compiler_id")
        digest = compiler.get("compile_commands_digest")
        if not isinstance(path, str) or not path.strip():
            raise ClangTypeOverlayError(f"{cctx} has empty compiler_path")
        if not isinstance(cid, str) or not cid.strip():
            raise ClangTypeOverlayError(f"{cctx} has empty compiler_id")
        if digest != expected_digest:
            raise ClangTypeOverlayError(
                f"{cctx} compile_commands_digest disagrees with the audit report"
            )
        identity = (path.strip(), cid.strip())
        if identity not in allowed_compilers:
            raise ClangTypeOverlayError(
                f"{cctx} names a compiler absent from report.compilers"
            )
        if identity in seen:
            raise ClangTypeOverlayError(
                f"{context} compilers contains a duplicate identity"
            )
        seen.add(identity)
        normalized.append(
            {
                "compiler_path": identity[0],
                "compiler_id": identity[1],
                "compile_commands_digest": expected_digest,
            }
        )
    normalized.sort(key=_canonical_json)
    return _canonical_json(normalized)


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


def _type_payload(
    row: Dict[str, Any],
    *,
    expected_digest: str,
    allowed_compilers: Set[Tuple[str, str]],
    entry_compilers: Dict[int, Tuple[str, str]],
    n_compile_entries: int,
) -> Dict[str, Any]:
    """Build the entity field dict for one confirmed matched audit row."""
    title = str(row.get("tree_sitter_title") or "")
    kind = row.get("entity_kind")
    if kind not in TYPE_ENTITY_KINDS:
        raise ClangTypeOverlayError(
            f"matched row {title!r} has invalid entity_kind {kind!r}"
        )
    if row.get("line_column_confirmed") is not True:
        raise ClangTypeOverlayError(
            f"matched row {title!r} lacks line_column_confirmed=true"
        )
    for field in (
        "graph_canonical_span",
        "matched_site_span",
        "location_origin",
    ):
        if not isinstance(row.get(field), str) or not str(row.get(field)).strip():
            raise ClangTypeOverlayError(
                f"matched row {title!r} has empty {field}"
            )
    _validate_site_coordinates(row, title=title)
    is_canonical = row["matched_site_is_canonical"]

    entry_indices = _entry_indices(
        row.get("entry_indices"),
        context=f"matched row {title!r}",
        n_compile_entries=n_compile_entries,
    )
    compilers_json = _canonical_compilers_json(
        row.get("compilers"),
        context=f"matched row {title!r}",
        expected_digest=expected_digest,
        allowed_compilers=allowed_compilers,
    )
    compilers = json.loads(compilers_json)
    row_compilers = {
        (compiler["compiler_path"], compiler["compiler_id"])
        for compiler in compilers
    }
    expected_row_compilers = {entry_compilers[index] for index in entry_indices}
    if row_compilers != expected_row_compilers:
        raise ClangTypeOverlayError(
            f"matched row {title!r} compiler provenance disagrees with its "
            "compile entries"
        )

    # Singular compiler identity only when the row is single-compiler.
    compiler_path = row.get("compiler_path")
    compiler_id = row.get("compiler_id")
    if len(compilers) == 1:
        only = compilers[0]
        if not isinstance(compiler_path, str) or not compiler_path.strip():
            raise ClangTypeOverlayError(
                f"matched row {title!r} lacks its singular compiler_path"
            )
        if not isinstance(compiler_id, str) or not compiler_id.strip():
            raise ClangTypeOverlayError(
                f"matched row {title!r} lacks its singular compiler_id"
            )
        if (compiler_path.strip(), compiler_id.strip()) != (
            only["compiler_path"],
            only["compiler_id"],
        ):
            raise ClangTypeOverlayError(
                f"matched row {title!r} singular compiler disagrees with "
                "compilers list"
            )
        compiler_path = compiler_path.strip()
        compiler_id = compiler_id.strip()
    elif compiler_path is not None or compiler_id is not None:
        raise ClangTypeOverlayError(
            f"matched row {title!r} exposes singular compiler fields for "
            "multiple compilers"
        )
    else:
        compiler_path = None
        compiler_id = None

    digest = row.get("compile_commands_digest")
    if not isinstance(digest, str) or not digest.strip():
        raise ClangTypeOverlayError(
            f"matched row {title!r} has empty compile_commands_digest"
        )
    if digest.strip() != expected_digest:
        raise ClangTypeOverlayError(
            f"matched row {title!r} compile_commands_digest disagrees with "
            "the audit report"
        )

    qual = _optional_type_string(
        row.get("qualType"), field="qualType", title=title
    )
    desugared = _optional_type_string(
        row.get("desugaredQualType"), field="desugaredQualType", title=title
    )
    fixed = _optional_type_string(
        row.get("fixedUnderlyingType"),
        field="fixedUnderlyingType",
        title=title,
    )

    desc = (
        f"configured Clang type declaration for {title} "
        f"(kind={kind}; fact_kind={FACT_KIND}; graph_canonical="
        f"{row.get('graph_canonical_span')}; matched_site="
        f"{row.get('matched_site_span')}; deterministic only relative to "
        f"recorded Clang + compile_commands.json)"
    )
    return {
        "clang_type_declaration_confirmed": True,
        "clang_type_fact_kind": FACT_KIND,
        "clang_type_extractor": EXTRACTOR,
        "clang_type_entity_kind": kind,
        "clang_type_qual_type": qual,
        "clang_type_desugared_qual_type": desugared,
        "clang_type_fixed_underlying_type": fixed,
        "clang_type_graph_canonical_span": str(row["graph_canonical_span"]),
        "clang_type_graph_canonical_line": int(row["graph_canonical_line"]),
        "clang_type_graph_canonical_col0": int(row["graph_canonical_col0"]),
        "clang_type_matched_site_span": str(row["matched_site_span"]),
        "clang_type_matched_site_line": int(row["matched_site_line"]),
        "clang_type_matched_site_col0": int(row["matched_site_col0"]),
        "clang_type_matched_site_is_canonical": is_canonical,
        "clang_type_location_origin": str(row["location_origin"]).strip(),
        "clang_type_entry_indices": entry_indices,
        "clang_type_compiler_path": compiler_path,
        "clang_type_compiler_id": compiler_id,
        "clang_type_compilers": compilers_json,
        "clang_type_compile_commands_digest": expected_digest,
        "clang_type_confidence": 1.0,
        "clang_type_is_deterministic": True,
        "clang_type_description": desc,
    }


def _validate_payload_against_entity(
    entity: Dict[str, Any], payload: Dict[str, Any], *, title: str
) -> None:
    for key, new_val in payload.items():
        if key in entity and not _values_compatible(entity.get(key), new_val):
            raise ClangTypeOverlayError(
                f"conflicting pre-existing type field {key!r} on entity "
                f"{title!r}: existing={entity.get(key)!r} new={new_val!r}"
            )


def _apply_payload_to_entity(
    entity: Dict[str, Any], payload: Dict[str, Any], *, title: str
) -> bool:
    """Write payload onto entity; return True if any field changed.

    Optional null fields (e.g. absent qualType) are written explicitly so
    reapplication stays idempotent and parquet columns stay stable.
    """
    _validate_payload_against_entity(entity, payload, title=title)
    changed = False
    for key, new_val in payload.items():
        if key not in entity or entity.get(key) != new_val:
            entity[key] = new_val
            changed = True
    return changed


def _index_type_entities(
    data: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Map type-entity title -> list of entity dicts (expect length 1)."""
    by_title: Dict[str, List[Dict[str, Any]]] = {}
    for e in data.get("entities") or []:
        if str(e.get("type")) not in TYPE_ENTITY_KINDS:
            continue
        title = str(e.get("title") or "")
        if not title:
            continue
        by_title.setdefault(title, []).append(e)
    return by_title


def _validated_report_rows(
    report: Dict[str, Any],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    counts_raw = report.get("counts")
    if not isinstance(counts_raw, dict):
        raise ClangTypeOverlayError("type audit report has no counts object")
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    counts: Dict[str, int] = {}
    for key in _ALL_REPORT_BUCKETS:
        raw_rows = report.get(key)
        if not isinstance(raw_rows, list) or not all(
            isinstance(row, dict) for row in raw_rows
        ):
            raise ClangTypeOverlayError(
                f"type audit report bucket {key!r} must be a list of objects"
            )
        raw_count = counts_raw.get(key)
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 0
        ):
            raise ClangTypeOverlayError(
                f"type audit report count {key!r} must be a non-negative integer"
            )
        if raw_count != len(raw_rows):
            raise ClangTypeOverlayError(
                f"type audit report count/list mismatch for {key!r}: "
                f"count={raw_count} rows={len(raw_rows)}"
            )
        buckets[key] = list(raw_rows)
        counts[key] = raw_count
    return buckets, counts


def _fail_closed_residuals(buckets: Dict[str, List[Dict[str, Any]]]) -> None:
    problems = [
        f"{key}={len(buckets[key])}"
        for key in _FAIL_CLOSED_BUCKETS
        if buckets[key]
    ]
    if problems:
        raise ClangTypeOverlayError(
            "clang type overlay refuses unclean type-audit residuals: "
            + ", ".join(problems)
            + "; resolve or leave --clang-types off"
        )


def apply_clang_types_from_report(
    data: Dict[str, List[Dict[str, Any]]],
    report: Dict[str, Any],
    package_dir: Path,
) -> Dict[str, Any]:
    """Apply a precomputed type-audit report onto ``data['entities']``.

    Pure relative to the report: does not invoke the compiler. Plans fully
    before mutating. Returns a manifest provenance block.
    """
    package_dir = Path(package_dir).resolve()
    if str(report.get("mode") or "") != "clang_ast_json_type_declaration_audit":
        raise ClangTypeOverlayError(
            f"unexpected type audit mode {report.get('mode')!r}; expected "
            "clang_ast_json_type_declaration_audit"
        )
    if str(report.get("package") or "") != package_dir.name:
        raise ClangTypeOverlayError(
            f"type audit report package {report.get('package')!r} does not "
            f"match the target package {package_dir.name!r}"
        )
    raw_digest = report.get("compile_commands_digest")
    if not isinstance(raw_digest, str) or not raw_digest.strip():
        raise ClangTypeOverlayError(
            "type audit report has empty compile_commands_digest"
        )
    digest = raw_digest.strip()
    n_compile_entries = report.get("n_compile_entries")
    translation_units = report.get("translation_units")
    if (
        isinstance(n_compile_entries, bool)
        or not isinstance(n_compile_entries, int)
        or n_compile_entries <= 0
    ):
        raise ClangTypeOverlayError(
            "type audit report n_compile_entries must be a positive integer"
        )
    if (
        not isinstance(translation_units, list)
        or not all(isinstance(row, dict) for row in translation_units)
        or len(translation_units) != n_compile_entries
    ):
        raise ClangTypeOverlayError(
            "type audit report translation_units must contain exactly one "
            "row per compile entry"
        )
    compilers = report.get("compilers")
    if (
        not isinstance(compilers, list)
        or not compilers
        or not all(isinstance(row, dict) for row in compilers)
    ):
        raise ClangTypeOverlayError(
            "type audit report compilers must be a list of provenance objects"
        )
    allowed_compilers: Set[Tuple[str, str]] = set()
    normalized_compilers: List[Dict[str, Any]] = []
    for position, compiler in enumerate(compilers):
        raw_path = compiler.get("compiler_path")
        raw_id = compiler.get("compiler_id")
        if (
            not isinstance(raw_path, str)
            or not raw_path.strip()
            or not isinstance(raw_id, str)
            or not raw_id.strip()
        ):
            raise ClangTypeOverlayError(
                f"type audit report compiler {position} has incomplete identity"
            )
        compiler_path = raw_path.strip()
        compiler_id = raw_id.strip()
        if not Path(compiler_path).is_absolute():
            raise ClangTypeOverlayError(
                f"type audit report compiler {position} path is not absolute: "
                f"{compiler_path!r}"
            )
        identity = (compiler_path, compiler_id)
        if identity in allowed_compilers:
            raise ClangTypeOverlayError(
                f"duplicate compiler identity in report.compilers: {identity!r}"
            )
        allowed_compilers.add(identity)
        normalized = dict(compiler)
        normalized["compiler_path"] = compiler_path
        normalized["compiler_id"] = compiler_id
        normalized_compilers.append(normalized)
    try:
        normalized_compilers.sort(key=_canonical_json)
    except (TypeError, ValueError) as e:
        raise ClangTypeOverlayError(
            f"type audit report compiler provenance is not JSON-serializable: "
            f"{e}"
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
            raise ClangTypeOverlayError(
                f"type audit translation unit {position} has invalid or "
                f"duplicate entry_index {entry_index!r}"
            )
        tu_path = translation_unit.get("compiler_path")
        tu_id = translation_unit.get("compiler_id")
        if not isinstance(tu_path, str) or not isinstance(tu_id, str):
            raise ClangTypeOverlayError(
                f"type audit translation unit {position} has incomplete "
                "compiler identity"
            )
        identity = (tu_path.strip(), tu_id.strip())
        if identity not in allowed_compilers:
            raise ClangTypeOverlayError(
                f"type audit translation unit {position} names a compiler "
                "absent from report.compilers"
            )
        entry_compilers[entry_index] = identity
    if set(entry_compilers) != set(range(n_compile_entries)):
        raise ClangTypeOverlayError(
            "type audit translation-unit entry indices are not a complete census"
        )

    # Singular top-level compiler identity must agree with compilers list.
    top_path = report.get("compiler_path")
    top_id = report.get("compiler_id")
    top_version = report.get("compiler_version")
    if len(allowed_compilers) == 1:
        only_path, only_id = next(iter(allowed_compilers))
        only_version = normalized_compilers[0].get("compiler_version")
        if (top_path, top_id) != (only_path, only_id):
            raise ClangTypeOverlayError(
                "singular compiler identity disagrees with report.compilers"
            )
        if top_version is not None and top_version != only_version:
            raise ClangTypeOverlayError(
                "singular compiler_version disagrees with report.compilers"
            )
    elif any(value is not None for value in (top_path, top_id, top_version)):
        raise ClangTypeOverlayError(
            "multi-compiler type audit report must not expose a singular "
            "compiler identity"
        )

    buckets, validated_counts = _validated_report_rows(report)
    _fail_closed_residuals(buckets)

    matched = buckets["matched"]
    unconfirmed = [
        r for r in matched if r.get("line_column_confirmed") is not True
    ]
    if unconfirmed:
        titles = sorted(
            str(r.get("tree_sitter_title") or r.get("name") or "?")
            for r in unconfirmed
        )
        raise ClangTypeOverlayError(
            "clang type overlay requires line_column_confirmed=true for all "
            f"matched rows; unconfirmed={titles[:10]}"
        )

    by_title = _index_type_entities(data)

    matched_sorted = sorted(
        matched,
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
            raise ClangTypeOverlayError(
                f"matched row missing tree_sitter_title: {row!r}"
            )
        title = raw_title
        if title in matched_titles:
            raise ClangTypeOverlayError(
                f"type audit report contains duplicate matched title {title!r}"
            )
        matched_titles.add(title)

        name = row.get("name")
        if (
            not isinstance(name, str)
            or not name
            or title.rsplit(":", 1)[-1] != name
        ):
            raise ClangTypeOverlayError(
                f"matched row title/name mismatch: title={title!r} name={name!r}"
            )
        kind = row.get("entity_kind")
        if kind not in TYPE_ENTITY_KINDS:
            raise ClangTypeOverlayError(
                f"matched row {title!r} has invalid entity_kind {kind!r}"
            )

        raw_rel_path = row.get("source_path")
        if not isinstance(raw_rel_path, str) or not raw_rel_path:
            raise ClangTypeOverlayError(
                f"matched row {title!r} missing source_path"
            )
        rel_path = raw_rel_path
        try:
            normalized_rel = package_relative_posix(
                (package_dir / Path(rel_path)).resolve(), package_dir
            )
        except (OSError, ValueError) as e:
            raise ClangTypeOverlayError(
                f"matched row {title!r} has invalid package-relative "
                f"source_path {rel_path!r}: {e}"
            ) from e
        if normalized_rel != rel_path:
            raise ClangTypeOverlayError(
                f"matched row {title!r} has non-canonical source_path "
                f"{rel_path!r}; expected {normalized_rel!r}"
            )

        ents = by_title.get(title) or []
        if len(ents) == 0:
            raise ClangTypeOverlayError(
                f"no type entity for matched title {title!r}"
            )
        if len(ents) > 1:
            raise ClangTypeOverlayError(
                f"non-unique type entity title {title!r} "
                f"({len(ents)} rows); refuse ambiguous attachment"
            )
        entity = ents[0]
        ent_type = str(entity.get("type") or "")
        if ent_type != kind:
            raise ClangTypeOverlayError(
                f"entity type mismatch for {title!r}: graph={ent_type!r} "
                f"audit={kind!r}"
            )
        symbol_name = entity.get("symbol_name")
        if not isinstance(symbol_name, str) or symbol_name != name:
            raise ClangTypeOverlayError(
                f"symbol_name mismatch for {title!r}: "
                f"graph={symbol_name!r} audit={name!r}"
            )

        ent_rel = _entity_rel_source(entity, package_dir)
        if ent_rel != rel_path:
            raise ClangTypeOverlayError(
                f"source-path mismatch for {title!r}: graph={ent_rel!r} "
                f"audit={rel_path!r}"
            )

        graph_span = str(entity.get("span") or "")
        canonical_span = str(row.get("graph_canonical_span") or "")
        if not graph_span or graph_span != canonical_span:
            raise ClangTypeOverlayError(
                f"canonical-span mismatch for {title!r}: graph={graph_span!r} "
                f"audit={canonical_span!r}"
            )

        # Base identity fields must not be rewritten by the payload.
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

        payload = _type_payload(
            row,
            expected_digest=digest,
            allowed_compilers=allowed_compilers,
            entry_compilers=entry_compilers,
            n_compile_entries=n_compile_entries,
        )
        # Refuse unknown pre-existing clang_type_* keys outside the namespace.
        unknown = sorted(
            str(k)
            for k, value in entity.items()
            if str(k).startswith("clang_type_")
            and str(k) not in _TYPE_FIELDS
            and value is not None
            and not (
                isinstance(value, float) and math.isnan(value)
            )
        )
        if unknown:
            raise ClangTypeOverlayError(
                f"unknown pre-existing clang_type_* fields on {title!r}: "
                f"{unknown}"
            )
        _validate_payload_against_entity(entity, payload, title=title)
        plans.append((entity, payload, title, base_snapshot))

    # Stale clang_type_* on unmatched or non-type entities fails closed.
    for entity in data.get("entities") or []:
        material_fields = [
            key
            for key in entity
            if str(key).startswith("clang_type_")
            and entity.get(key) is not None
            and not (
                isinstance(entity.get(key), float)
                and math.isnan(entity.get(key))
            )
        ]
        if not material_fields:
            continue
        title = str(entity.get("title") or "")
        if str(entity.get("type")) not in TYPE_ENTITY_KINDS:
            raise ClangTypeOverlayError(
                f"non-type entity {title!r} carries Clang type fields: "
                f"{sorted(material_fields)}"
            )
        if title not in matched_titles:
            raise ClangTypeOverlayError(
                f"stale Clang type fields on unmatched entity {title!r}: "
                f"{sorted(material_fields)}"
            )

    # Mutation begins only after every row/entity/path/provenance check.
    n_changed = 0
    for entity, payload, title, base in plans:
        if _apply_payload_to_entity(entity, payload, title=title):
            n_changed += 1
        for key, expected in base.items():
            if entity.get(key) != expected:
                raise ClangTypeOverlayError(
                    f"internal error: base field {key!r} mutated on {title!r}"
                )

    return {
        "mode": MODE,
        "enabled": True,
        "fact_kind": FACT_KIND,
        "extractor": EXTRACTOR,
        "n_facts": len(plans),
        "n_facts_changed": n_changed,
        "n_compile_entries": n_compile_entries,
        "n_translation_units": len(translation_units),
        "compiler_path": report.get("compiler_path"),
        "compiler_id": report.get("compiler_id"),
        "compiler_version": report.get("compiler_version"),
        "compilers": normalized_compilers,
        "compile_commands_digest": digest,
        "counts": {
            key: validated_counts[key] for key in _ALL_REPORT_BUCKETS
        },
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }


def append_clang_types(
    data: Dict[str, List[Dict[str, Any]]],
    package_dir: Path,
    *,
    timeout: int = 120,
    report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the type audit (unless ``report`` given) and attach type evidence.

    Mutates ``data['entities']`` in place. Returns the manifest provenance
    block for ``extra_manifest['clang_types']``. Prefer passing a report built
    via ``build_type_declaration_audit_from_capture`` so index_c can share one
    AST capture with the other Clang overlays.
    """
    package_dir = Path(package_dir).resolve()
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout <= 0
    ):
        raise ClangTypeOverlayError(
            f"timeout must be a positive integer, got {timeout!r}"
        )
    if report is None:
        try:
            report = run_clang_type_audit(package_dir, timeout=timeout)
        except ClangTypeAuditError as e:
            raise ClangTypeOverlayError(str(e)) from e
    return apply_clang_types_from_report(data, report, package_dir)


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
        "stale_type_metadata",
        "partial_type_payload",
        "unknown_type_field",
        "type_field_type",
        "identity_mismatch",
        "canonical_span_mismatch",
        "graph_span_mismatch",
        "matched_span_mismatch",
        "canonical_site_marker",
        "optional_type_string",
        "entry_index_census",
        "compiler_json",
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

_OPTIONAL_TYPE_STRING_FIELDS = (
    "clang_type_qual_type",
    "clang_type_desugared_qual_type",
    "clang_type_fixed_underlying_type",
)

_SINGULAR_COMPILER_FIELDS = (
    "clang_type_compiler_path",
    "clang_type_compiler_id",
)

# Affirmative proof claims that must never appear in persisted type evidence.
# Honest producer text only ever *denies* these.
_FORBIDDEN_CLAIM_PATTERNS = (
    re.compile(r"\bABI[- ](?:proof|proven|guarantee[ds]?|compatib\w*)\b", re.I),
    re.compile(r"\blayout[- ](?:proof|proven|guarantee[ds]?|compatib\w*)\b", re.I),
    re.compile(r"\btype[- ]use[- ](?:proof|proven|guarantee[ds]?)\b", re.I),
    re.compile(r"\bmulti-config\s+(?:proof|coverage\s+guaranteed)\b", re.I),
    re.compile(r"\bmacro[- ]complete(?:\s+proof)?\b", re.I),
    re.compile(
        r"\b(?:proves|proven|guarantee[ds]?|verifie[ds])\s+"
        r"(?:the\s+)?(?:ABI|layout|type[- ]use|multi-config|macro[- ]complete)\b",
        re.I,
    ),
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
        raise AssertionError(f"unknown type integrity anomaly code {code!r}")
    row: Dict[str, Any] = {"code": code, "message": _clip(message)}
    if entity_id is not None:
        row["entity_id"] = entity_id
    if extra:
        for key, value in sorted(extra.items()):
            row[key] = _clip(value) if isinstance(value, str) else value
    return row


def _forbidden_claims(text: Any) -> List[str]:
    """Affirmative ABI/layout/type-use/multi-config/macro-complete claims."""
    if not isinstance(text, str):
        return []
    return sorted(
        {
            pattern.search(text).group(0)  # type: ignore[union-attr]
            for pattern in _FORBIDDEN_CLAIM_PATTERNS
            if pattern.search(text)
        }
    )


def _has_material_type_fields(entity: Any) -> bool:
    for key in _row_keys(entity):
        if key.startswith("clang_type_") and is_material_value(
            _row_get(entity, key)
        ):
            return True
    return False


def _span_start_coords(value: Any) -> Optional[Tuple[int, int]]:
    if not isinstance(value, str):
        return None
    match = _SPAN_START_RE.match(value)
    if match is None:
        return None
    return int(match.group("line")), int(match.group("col"))


def _decoded_compiler_json(
    raw: Any,
    *,
    entity_id: Optional[str],
    anomalies: List[Dict[str, Any]],
) -> Any:
    """Strictly decode persisted ``clang_type_compilers`` canonical JSON."""
    if not isinstance(raw, str) or not raw:
        anomalies.append(
            _anomaly(
                "compiler_json",
                "clang_type_compilers is not a non-empty JSON string: "
                f"{type(raw).__name__}",
                entity_id=entity_id,
            )
        )
        return None
    try:
        decoded = strict_json_loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        anomalies.append(
            _anomaly(
                "compiler_json",
                f"clang_type_compilers is not strict JSON: {error}",
                entity_id=entity_id,
            )
        )
        return None
    if _contains_non_finite(decoded):
        anomalies.append(
            _anomaly(
                "compiler_json",
                "clang_type_compilers contains NaN/Infinity",
                entity_id=entity_id,
            )
        )
        return None
    try:
        canonical = _strict_canonical_json(decoded)
    except (TypeError, ValueError) as error:
        anomalies.append(
            _anomaly(
                "compiler_json",
                f"clang_type_compilers is not canonical-encodable: {error}",
                entity_id=entity_id,
            )
        )
        return None
    if canonical != raw:
        anomalies.append(
            _anomaly(
                "compiler_json",
                "clang_type_compilers is not producer-canonical JSON",
                entity_id=entity_id,
            )
        )
        return None
    return decoded


def _validate_decorated_entity(
    entity: Any,
    *,
    entity_id: Optional[str],
    manifest_digest: Optional[str],
    manifest_compilers: Set[Tuple[str, str]],
    n_compile_entries: Optional[int],
    anomalies: List[Dict[str, Any]],
) -> None:
    """Validate one decorated type-declaration entity."""
    row_keys = set(_row_keys(entity))
    material = {
        key
        for key in row_keys
        if key.startswith("clang_type_")
        and is_material_value(_row_get(entity, key))
    }
    unknown = sorted(material - set(_TYPE_FIELDS))
    if unknown:
        anomalies.append(
            _anomaly(
                "unknown_type_field",
                f"unknown clang_type_* fields: {unknown}",
                entity_id=entity_id,
            )
        )
    optional_null = set(_OPTIONAL_TYPE_STRING_FIELDS) | set(
        _SINGULAR_COMPILER_FIELDS
    )
    missing_keys = {field for field in _TYPE_FIELDS if field not in row_keys}
    null_required = {
        field
        for field in _TYPE_FIELDS
        if field not in optional_null and field not in material
    }
    missing = sorted(missing_keys | null_required)
    if missing:
        anomalies.append(
            _anomaly(
                "partial_type_payload",
                f"decorated entity is missing required fields: {missing}",
                entity_id=entity_id,
            )
        )

    if _as_bool(_row_get(entity, "clang_type_declaration_confirmed")) is not True:
        anomalies.append(
            _anomaly(
                "type_field_type",
                "clang_type_declaration_confirmed is not boolean true",
                entity_id=entity_id,
            )
        )
    if _row_get(entity, "clang_type_fact_kind") != FACT_KIND:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_type_fact_kind="
                f"{_row_get(entity, 'clang_type_fact_kind')!r} expected "
                f"{FACT_KIND!r}",
                entity_id=entity_id,
            )
        )
    if _row_get(entity, "clang_type_extractor") != EXTRACTOR:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_type_extractor="
                f"{_row_get(entity, 'clang_type_extractor')!r} expected "
                f"{EXTRACTOR!r}",
                entity_id=entity_id,
            )
        )
    entity_type = str(_row_get(entity, "type") or "")
    type_kind = _row_get(entity, "clang_type_entity_kind")
    if type_kind != entity_type:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_type_entity_kind={type_kind!r} != entity type "
                f"{entity_type!r}",
                entity_id=entity_id,
            )
        )
    graph_span = _row_get(entity, "clang_type_graph_canonical_span")
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
                f"clang_type_graph_canonical_span={graph_span!r} != entity "
                f"span {entity_span!r}",
                entity_id=entity_id,
            )
        )

    graph_line = _as_int(_row_get(entity, "clang_type_graph_canonical_line"))
    graph_col = _as_int(_row_get(entity, "clang_type_graph_canonical_col0"))
    if graph_line is None or graph_line < 1:
        anomalies.append(
            _anomaly(
                "type_field_type",
                f"clang_type_graph_canonical_line="
                f"{_row_get(entity, 'clang_type_graph_canonical_line')!r} "
                "is not a positive integer",
                entity_id=entity_id,
            )
        )
    if graph_col is None or graph_col < 0:
        anomalies.append(
            _anomaly(
                "type_field_type",
                f"clang_type_graph_canonical_col0="
                f"{_row_get(entity, 'clang_type_graph_canonical_col0')!r} "
                "is not a non-negative integer",
                entity_id=entity_id,
            )
        )
    graph_coords = _span_start_coords(graph_span)
    if (
        graph_coords is None
        or graph_line is None
        or graph_col is None
        or graph_coords != (graph_line, graph_col)
    ):
        anomalies.append(
            _anomaly(
                "graph_span_mismatch",
                f"graph canonical span={graph_span!r} disagrees with "
                f"line={graph_line!r} col0={graph_col!r}",
                entity_id=entity_id,
            )
        )

    matched_span = _row_get(entity, "clang_type_matched_site_span")
    matched_line = _as_int(_row_get(entity, "clang_type_matched_site_line"))
    matched_col = _as_int(_row_get(entity, "clang_type_matched_site_col0"))
    if not isinstance(matched_span, str) or not matched_span.strip():
        anomalies.append(
            _anomaly(
                "type_field_type",
                "clang_type_matched_site_span is not a non-empty string",
                entity_id=entity_id,
            )
        )
    if matched_line is None or matched_line < 1:
        anomalies.append(
            _anomaly(
                "type_field_type",
                f"clang_type_matched_site_line="
                f"{_row_get(entity, 'clang_type_matched_site_line')!r} "
                "is not a positive integer",
                entity_id=entity_id,
            )
        )
    if matched_col is None or matched_col < 0:
        anomalies.append(
            _anomaly(
                "type_field_type",
                f"clang_type_matched_site_col0="
                f"{_row_get(entity, 'clang_type_matched_site_col0')!r} "
                "is not a non-negative integer",
                entity_id=entity_id,
            )
        )
    matched_coords = _span_start_coords(matched_span)
    if (
        matched_coords is None
        or matched_line is None
        or matched_col is None
        or matched_coords != (matched_line, matched_col)
    ):
        anomalies.append(
            _anomaly(
                "matched_span_mismatch",
                f"matched-site span={matched_span!r} disagrees with "
                f"line={matched_line!r} col0={matched_col!r}",
                entity_id=entity_id,
            )
        )

    same_site = (
        isinstance(graph_span, str)
        and isinstance(matched_span, str)
        and graph_span == matched_span
        and graph_line is not None
        and matched_line is not None
        and graph_col is not None
        and matched_col is not None
        and (graph_line, graph_col) == (matched_line, matched_col)
    )
    is_canonical = _as_bool(
        _row_get(entity, "clang_type_matched_site_is_canonical")
    )
    if is_canonical is None:
        anomalies.append(
            _anomaly(
                "type_field_type",
                "clang_type_matched_site_is_canonical is not boolean",
                entity_id=entity_id,
            )
        )
    elif is_canonical is not same_site:
        anomalies.append(
            _anomaly(
                "canonical_site_marker",
                f"clang_type_matched_site_is_canonical={is_canonical!r} "
                f"but canonical/matched sites equal={same_site}",
                entity_id=entity_id,
            )
        )

    for field in _OPTIONAL_TYPE_STRING_FIELDS:
        value = _row_get(entity, field)
        if not is_material_value(value):
            continue
        if not isinstance(value, str) or not value.strip():
            anomalies.append(
                _anomaly(
                    "optional_type_string",
                    f"{field} must be a non-empty string or null: {value!r}",
                    entity_id=entity_id,
                )
            )

    origin = _row_get(entity, "clang_type_location_origin")
    if not isinstance(origin, str) or not origin.strip():
        anomalies.append(
            _anomaly(
                "type_field_type",
                "clang_type_location_origin is not a non-empty string",
                entity_id=entity_id,
            )
        )

    if not _is_one(_row_get(entity, "clang_type_confidence")):
        anomalies.append(
            _anomaly(
                "type_field_type",
                f"clang_type_confidence="
                f"{_row_get(entity, 'clang_type_confidence')!r} expected 1.0",
                entity_id=entity_id,
            )
        )
    if _as_bool(_row_get(entity, "clang_type_is_deterministic")) is not True:
        anomalies.append(
            _anomaly(
                "type_field_type",
                "clang_type_is_deterministic is not boolean true",
                entity_id=entity_id,
            )
        )

    entry_indices = _normalize_list_field(
        _row_get(entity, "clang_type_entry_indices")
    )
    if entry_indices is None or not entry_indices:
        anomalies.append(
            _anomaly(
                "entry_index_census",
                "clang_type_entry_indices is not a non-empty list",
                entity_id=entity_id,
            )
        )
    else:
        decoded_indices = [_as_int(index) for index in entry_indices]
        if any(index is None or index < 0 for index in decoded_indices):
            anomalies.append(
                _anomaly(
                    "entry_index_census",
                    f"clang_type_entry_indices has non-integer entries: "
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
                        f"clang_type_entry_indices is not sorted/unique: "
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
                        f"clang_type_entry_indices {values!r} outside manifest "
                        f"compile-entry census (n={n_compile_entries})",
                        entity_id=entity_id,
                    )
                )

    digest = _row_get(entity, "clang_type_compile_commands_digest")
    if not isinstance(digest, str) or not digest.strip():
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"clang_type_compile_commands_digest is empty: {digest!r}",
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

    compilers_decoded = _decoded_compiler_json(
        _row_get(entity, "clang_type_compilers"),
        entity_id=entity_id,
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
                    "clang_type_compilers must be a non-empty list of objects",
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
                            f"clang_type_compilers[{position}] keys "
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
                    or not Path(path.strip()).is_absolute()
                    or not isinstance(cid, str)
                    or not cid.strip()
                ):
                    anomalies.append(
                        _anomaly(
                            "compiler_mismatch",
                            f"clang_type_compilers[{position}] has incomplete "
                            "or non-absolute identity",
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
                            f"clang_type_compilers[{position}] digest disagrees "
                            "with the entity digest",
                            entity_id=entity_id,
                        )
                    )
                identity = (path.strip(), cid.strip())
                if identity in identities:
                    anomalies.append(
                        _anomaly(
                            "compiler_mismatch",
                            f"clang_type_compilers contains duplicate "
                            f"{identity!r}",
                            entity_id=entity_id,
                        )
                    )
                    continue
                identities.append(identity)
            if manifest_compilers:
                outside = sorted(set(identities) - manifest_compilers)
                if outside:
                    anomalies.append(
                        _anomaly(
                            "compiler_mismatch",
                            f"entity compilers absent from manifest census: "
                            f"{outside!r}",
                            entity_id=entity_id,
                        )
                    )

    singular_path = _row_get(entity, "clang_type_compiler_path")
    singular_id = _row_get(entity, "clang_type_compiler_id")
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
                    "clang_type_compilers",
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

    description = _row_get(entity, "clang_type_description")
    if not isinstance(description, str) or not description.strip():
        anomalies.append(
            _anomaly(
                "confidence_boundary",
                "clang_type_description is empty",
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
                    f"clang_type_description drops its evidence boundary: "
                    f"{absent}",
                    entity_id=entity_id,
                )
            )
        claims = _forbidden_claims(description)
        if claims:
            anomalies.append(
                _anomaly(
                    "forbidden_claim",
                    f"clang_type_description claims {claims}",
                    entity_id=entity_id,
                )
            )


def _validate_type_manifest_block(
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
        for key in _ALL_REPORT_BUCKETS:
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
        if "matched" in counts and counts["matched"] != n_decorated:
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    f"manifest counts.matched={counts['matched']} "
                    f"!= {n_decorated} decorated entities",
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
                "entities",
            )
        )
    n_changed = block.get("n_facts_changed")
    upper = n_facts if isinstance(n_facts, int) and not isinstance(n_facts, bool) else n_decorated
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

    if block.get("confidence_boundary") != CONFIDENCE_BOUNDARY:
        anomalies.append(
            _anomaly(
                "manifest_contract_claim",
                "manifest confidence_boundary differs from the producer contract",
            )
        )
        claims = _forbidden_claims(block.get("confidence_boundary"))
        if claims:
            anomalies.append(
                _anomaly(
                    "forbidden_claim",
                    f"manifest confidence_boundary claims {claims}",
                )
            )
    extra_claim_text: List[Any] = []
    for field in ("limitations", "hard_equality", "description", "notes"):
        extra_claim_text.append(block.get(field))
    claims = sorted(
        {
            claim
            for text in extra_claim_text
            for claim in (
                _forbidden_claims(text)
                if isinstance(text, str)
                else [
                    c
                    for item in (text if isinstance(text, list) else [])
                    for c in _forbidden_claims(item)
                ]
            )
        }
    )
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


def validate_persisted_type_overlay(
    entities: Any,
    manifest: Optional[Any] = None,
    *,
    max_anomaly_samples: int = _MAX_ANOMALY_SAMPLES,
) -> Dict[str, Any]:
    """Validate already-persisted ``clang_type_*`` entity evidence.

    Pure and non-mutating. Never invokes Clang, loads compile_commands.json,
    reindexes, or rewrites graphs.

    Compatibility states:
      * no ``clang_types`` block and zero type fields -> ``legacy_absent``
      * ``mode=off`` / ``enabled=false`` -> requires zero type fields
      * enabled ``configured_clang_type_declarations`` -> full census

    A missing manifest block never legitimizes existing ``clang_type_*``
    fields. A present ``clang_types`` key that is null/list/string is
    invalid, not legacy. Nothing is ever repaired.
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
        if not _has_material_type_fields(entity):
            continue
        carrying.append(entity)
        entity_id = str(_row_get(entity, "id") or "") or None
        if str(_row_get(entity, "type") or "") in TYPE_ENTITY_KINDS:
            decorated.append(entity)
        else:
            anomalies.append(
                _anomaly(
                    "stale_type_metadata",
                    f"non-type entity carries clang_type_* fields "
                    f"(type={_row_get(entity, 'type')!r})",
                    entity_id=entity_id,
                )
            )

    has_block = "clang_types" in manifest_obj
    block = manifest_obj.get("clang_types")
    mode_state = "legacy_absent"
    block_enabled = False

    if not has_block:
        if carrying:
            anomalies.append(
                _anomaly(
                    "legacy_block_missing_with_fields",
                    f"manifest lacks clang_types but graph has "
                    f"{len(carrying)} entity/entities with clang_type_* fields",
                    extra={"n_entities": len(carrying)},
                )
            )
            mode_state = "invalid"
    elif not isinstance(block, dict):
        anomalies.append(
            _anomaly(
                "invalid_enabled_block",
                f"clang_types manifest block is not an object: "
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
                        f"clang_types is off/disabled but graph has "
                        f"{len(carrying)} entity/entities with clang_type_* "
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
                    f"clang_types enablement inconsistent: mode={mode!r} "
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
        ) = _validate_type_manifest_block(
            block,
            n_decorated=len(decorated),
            n_entities=len(entities_list),
            manifest_obj=manifest_obj,
            anomalies=anomalies,
        )

    if block_enabled or (mode_state == "invalid" and carrying):
        for entity in sorted(
            decorated,
            key=lambda e: (
                str(_row_get(e, "title") or ""),
                str(_row_get(e, "id") or ""),
            ),
        ):
            _validate_decorated_entity(
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
        "n_type_field_carriers": len(carrying),
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
