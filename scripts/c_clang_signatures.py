#!/usr/bin/env python
"""Optional Clang-derived configured function-signature overlay.

Enriches existing tree-sitter-c **function** entities with configuration /
toolchain-derived Clang signature fields from ``run_clang_ast_audit``.

This is **not**:
  * a new entity/relationship layer
  * full type resolution, ABI verification, or multi-config coverage
  * silent application of ambiguous / unconfirmed / residual audit rows

Only ``matched`` audit rows with ``line_column_confirmed=true`` and a unique
graph entity (collision-safe title + package-relative source path) are applied.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from c_clang_ast_audit import (  # type: ignore
    ClangAstAuditError,
    run_clang_ast_audit,
)
from c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    build_disabled_overlay_provenance,
)
from c_identities import package_relative_posix  # type: ignore

MODE = "clang_ast_signatures"
FACT_KIND = "configured_function_signature"
EXTRACTOR = "clang-ast-json"
CONFIDENCE_BOUNDARY = (
    "clang_signature_confidence=1.0 and clang_signature_is_deterministic=true "
    "mean the signature is re-derivable from the recorded Clang + "
    "compile_commands.json configuration via the AST function-definition audit, "
    "not that it is pure syntax, ABI-complete, multi-config, or free of "
    "macro/compile-scope residuals. Residuals (tree_sitter_only, "
    "out_of_compile_db_scope) receive no invented signatures."
)

# Fields written onto function entities (scalar / list / JSON text).
_SIGNATURE_FIELDS = (
    "clang_signature_status",
    "clang_qual_type",
    "clang_storage_class",
    "clang_inline",
    "clang_variadic",
    "clang_mangled_name",
    "clang_location_origin",
    "clang_signature_fact_kind",
    "clang_signature_extractor",
    "clang_signature_confidence",
    "clang_signature_is_deterministic",
    "clang_signature_compiler_path",
    "clang_signature_compiler_id",
    "clang_signature_compile_commands_digest",
    "clang_signature_entry_indices",
    "clang_signature_observations_json",
    "clang_signature_description",
)


class ClangSignatureError(CompilerOverlayError):
    """Raised when the signature overlay cannot apply honestly."""


def build_disabled_provenance() -> Dict[str, Any]:
    """Stable manifest block when ``--clang-signatures`` is off."""
    return build_disabled_overlay_provenance()


def _entity_rel_source(entity: Dict[str, Any], package_dir: Path) -> str:
    """Package-relative POSIX path for an entity ``source_file``."""
    package_dir = package_dir.resolve()
    raw = str(entity.get("source_file") or "")
    if not raw:
        raise ClangSignatureError(
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
        raise ClangSignatureError(
            f"cannot normalize source_file {raw!r} for entity "
            f"{entity.get('title')!r}: {e}"
        ) from e


def _entry_indices(value: Any, *, context: str) -> List[int]:
    """Validate one non-empty list of non-negative compile-entry indices."""
    if not isinstance(value, list) or not value:
        raise ClangSignatureError(
            f"{context} must have a non-empty list entry_indices"
        )
    normalized: Set[int] = set()
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            raise ClangSignatureError(
                f"{context} has invalid compile entry index {raw!r}"
            )
        normalized.add(raw)
    return sorted(normalized)


def _canonical_observations_json(
    row: Dict[str, Any],
    *,
    entry_indices: List[int],
    qual_type: str,
    expected_digest: str,
    allowed_compilers: Set[Tuple[str, str]],
) -> str:
    """Validate and deep-sort all observation provenance as canonical JSON."""
    obs = row.get("observations")
    if not isinstance(obs, list) or not obs:
        raise ClangSignatureError(
            f"matched row {row.get('tree_sitter_title')!r} must preserve a "
            "non-empty observations list"
        )
    normalized: List[Dict[str, Any]] = []
    observed_indices: Set[int] = set()
    for position, raw in enumerate(obs):
        context = (
            f"observation {position} for matched row "
            f"{row.get('tree_sitter_title')!r}"
        )
        if not isinstance(raw, dict):
            raise ClangSignatureError(f"{context} is not an object")
        record = dict(raw)
        indices = _entry_indices(record.get("entry_indices"), context=context)
        record["entry_indices"] = indices
        observed_indices.update(indices)
        for field in (
            "compiler_path",
            "compiler_id",
            "compile_commands_digest",
            "qualType",
        ):
            if not str(record.get(field) or "").strip():
                raise ClangSignatureError(f"{context} has empty {field}")
        if str(record.get("qualType") or "").strip() != qual_type:
            raise ClangSignatureError(
                f"{context} qualType disagrees with the matched row"
            )
        if str(record.get("compile_commands_digest")) != expected_digest:
            raise ClangSignatureError(
                f"{context} compile_commands_digest disagrees with the "
                "audit report"
            )
        compiler_key = (
            str(record.get("compiler_path")),
            str(record.get("compiler_id")),
        )
        if compiler_key not in allowed_compilers:
            raise ClangSignatureError(
                f"{context} names a compiler absent from report.compilers"
            )
        normalized.append(record)
    if observed_indices != set(entry_indices):
        raise ClangSignatureError(
            f"matched row {row.get('tree_sitter_title')!r} entry_indices "
            "disagree with its observation provenance"
        )
    try:
        normalized.sort(
            key=lambda item: json.dumps(
                item, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
        )
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
    except (TypeError, ValueError) as e:
        raise ClangSignatureError(
            f"matched row {row.get('tree_sitter_title')!r} has "
            f"non-JSON observation provenance: {e}"
        ) from e


def _signature_payload(
    row: Dict[str, Any],
    *,
    expected_digest: str,
    allowed_compilers: Set[Tuple[str, str]],
) -> Dict[str, Any]:
    """Build the entity field dict for one confirmed matched audit row."""
    raw_qual = row.get("qualType")
    if not isinstance(raw_qual, str) or not raw_qual.strip():
        raise ClangSignatureError(
            f"matched row {row.get('tree_sitter_title')!r} has empty qualType"
        )
    qual = raw_qual.strip()
    if row.get("line_column_confirmed") is not True:
        raise ClangSignatureError(
            f"matched row {row.get('tree_sitter_title')!r} lacks "
            "line_column_confirmed=true"
        )
    for field in ("inline", "variadic"):
        value = row.get(field)
        if value is not None and not isinstance(value, bool):
            raise ClangSignatureError(
                f"matched row {row.get('tree_sitter_title')!r} has "
                f"non-boolean {field}"
            )
    for field in ("storageClass", "mangledName"):
        value = row.get(field)
        if value is not None and not isinstance(value, str):
            raise ClangSignatureError(
                f"matched row {row.get('tree_sitter_title')!r} has "
                f"non-string {field}"
            )
    if not isinstance(row.get("location_origin"), str) or not str(
        row.get("location_origin")
    ).strip():
        raise ClangSignatureError(
            f"matched row {row.get('tree_sitter_title')!r} has empty "
            "location_origin"
        )
    title = str(row.get("tree_sitter_title") or "")
    entry_indices = _entry_indices(
        row.get("entry_indices"), context=f"matched row {title!r}"
    )
    for field in (
        "compiler_path",
        "compiler_id",
        "compile_commands_digest",
    ):
        if not isinstance(row.get(field), str) or not row.get(field).strip():
            raise ClangSignatureError(f"matched row {title!r} has empty {field}")
    if str(row.get("compile_commands_digest")) != expected_digest:
        raise ClangSignatureError(
            f"matched row {title!r} compile_commands_digest disagrees "
            "with the audit report"
        )
    if (
        str(row.get("compiler_path")),
        str(row.get("compiler_id")),
    ) not in allowed_compilers:
        raise ClangSignatureError(
            f"matched row {title!r} names a compiler absent from "
            "report.compilers"
        )
    desc = (
        f"configured Clang function signature for {title}: {qual} "
        f"(fact_kind={FACT_KIND}; deterministic only relative to recorded "
        f"Clang + compile_commands.json)"
    )
    return {
        "clang_signature_status": "matched",
        "clang_qual_type": qual,
        "clang_storage_class": row.get("storageClass"),
        "clang_inline": row.get("inline"),
        "clang_variadic": row.get("variadic"),
        "clang_mangled_name": row.get("mangledName"),
        "clang_location_origin": row.get("location_origin"),
        "clang_signature_fact_kind": FACT_KIND,
        "clang_signature_extractor": EXTRACTOR,
        "clang_signature_confidence": 1.0,
        "clang_signature_is_deterministic": True,
        "clang_signature_compiler_path": row.get("compiler_path"),
        "clang_signature_compiler_id": row.get("compiler_id"),
        "clang_signature_compile_commands_digest": row.get(
            "compile_commands_digest"
        ),
        "clang_signature_entry_indices": entry_indices,
        "clang_signature_observations_json": _canonical_observations_json(
            row,
            entry_indices=entry_indices,
            qual_type=qual,
            expected_digest=expected_digest,
            allowed_compilers=allowed_compilers,
        ),
        "clang_signature_description": desc,
    }


def _values_compatible(existing: Any, new: Any) -> bool:
    """True when existing is missing-data or type-compatibly equal to new."""
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
            return [json.dumps(x, sort_keys=True) for x in existing] == [
                json.dumps(x, sort_keys=True) for x in new
            ]
        except TypeError:
            return list(existing) == list(new)
    return type(existing) is type(new) and existing == new


def _validate_payload_against_entity(
    entity: Dict[str, Any], payload: Dict[str, Any], *, title: str
) -> None:
    """Preflight every payload field without mutating the entity."""
    for key, new_val in payload.items():
        if key in entity and not _values_compatible(entity.get(key), new_val):
            raise ClangSignatureError(
                f"conflicting pre-existing signature field {key!r} on entity "
                f"{title!r}: existing={entity.get(key)!r} new={new_val!r}"
            )


def _apply_payload_to_entity(
    entity: Dict[str, Any], payload: Dict[str, Any], *, title: str
) -> bool:
    """Write payload onto entity; return True if any field changed.

    Validates **all** fields for conflicts before writing any, so a failure
    never partially mutates the entity.
    """
    _validate_payload_against_entity(entity, payload, title=title)
    changed = False
    for key, new_val in payload.items():
        if entity.get(key) != new_val:
            entity[key] = new_val
            changed = True
    return changed


def _index_function_entities(
    data: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Map function title -> list of entity dicts (expect length 1)."""
    by_title: Dict[str, List[Dict[str, Any]]] = {}
    for e in data.get("entities") or []:
        if str(e.get("type")) != "function":
            continue
        title = str(e.get("title") or "")
        if not title:
            continue
        by_title.setdefault(title, []).append(e)
    return by_title


def _validated_report_rows(
    report: Dict[str, Any],
) -> Tuple[Dict[str, List[Dict[str, Any]]], Dict[str, int]]:
    """Validate report bucket types/counts before trusting manifest metadata."""
    counts_raw = report.get("counts")
    if not isinstance(counts_raw, dict):
        raise ClangSignatureError("audit report has no counts object")
    keys = (
        "matched",
        "tree_sitter_only",
        "clang_only",
        "ambiguous",
        "macro_location_unsupported",
        "out_of_compile_db_scope",
    )
    buckets: Dict[str, List[Dict[str, Any]]] = {}
    counts: Dict[str, int] = {}
    for key in keys:
        raw_rows = report.get(key)
        if not isinstance(raw_rows, list) or not all(
            isinstance(row, dict) for row in raw_rows
        ):
            raise ClangSignatureError(
                f"audit report bucket {key!r} must be a list of objects"
            )
        raw_count = counts_raw.get(key)
        if (
            isinstance(raw_count, bool)
            or not isinstance(raw_count, int)
            or raw_count < 0
        ):
            raise ClangSignatureError(
                f"audit report count {key!r} must be a non-negative integer"
            )
        if raw_count != len(raw_rows):
            raise ClangSignatureError(
                f"audit report count/list mismatch for {key!r}: "
                f"count={raw_count} rows={len(raw_rows)}"
            )
        buckets[key] = list(raw_rows)
        counts[key] = raw_count
    return buckets, counts


def _fail_closed_residuals(
    buckets: Dict[str, List[Dict[str, Any]]]
) -> None:
    """Refuse the overlay when the audit is not clean enough to publish."""
    bad_keys = ("clang_only", "ambiguous", "macro_location_unsupported")
    problems = [f"{key}={len(buckets[key])}" for key in bad_keys if buckets[key]]
    if problems:
        raise ClangSignatureError(
            "clang signature overlay refuses unclean audit residuals: "
            + ", ".join(problems)
            + "; resolve or leave --clang-signatures off"
        )


def apply_clang_signatures_from_report(
    data: Dict[str, List[Dict[str, Any]]],
    report: Dict[str, Any],
    package_dir: Path,
) -> Dict[str, Any]:
    """Apply a precomputed audit report onto ``data['entities']``.

    Pure relative to the report: does not invoke the compiler. Mutates entities
    in place. Returns a manifest provenance block.
    """
    package_dir = Path(package_dir).resolve()
    if str(report.get("mode") or "") != "clang_ast_json_audit":
        raise ClangSignatureError(
            f"unexpected audit mode {report.get('mode')!r}; expected "
            "clang_ast_json_audit"
        )
    if str(report.get("package") or "") != package_dir.name:
        raise ClangSignatureError(
            f"audit report package {report.get('package')!r} does not match "
            f"the target package {package_dir.name!r}"
        )
    raw_digest = report.get("compile_commands_digest")
    if not isinstance(raw_digest, str) or not raw_digest.strip():
        raise ClangSignatureError("audit report has empty compile_commands_digest")
    digest = raw_digest.strip()
    n_compile_entries = report.get("n_compile_entries")
    translation_units = report.get("translation_units")
    if (
        isinstance(n_compile_entries, bool)
        or not isinstance(n_compile_entries, int)
        or n_compile_entries <= 0
    ):
        raise ClangSignatureError(
            "audit report n_compile_entries must be a positive integer"
        )
    if (
        not isinstance(translation_units, list)
        or not all(isinstance(row, dict) for row in translation_units)
        or len(translation_units) != n_compile_entries
    ):
        raise ClangSignatureError(
            "audit report translation_units must contain exactly one row per "
            "compile entry"
        )
    compilers = report.get("compilers")
    if (
        not isinstance(compilers, list)
        or not compilers
        or not all(isinstance(row, dict) for row in compilers)
    ):
        raise ClangSignatureError(
            "audit report compilers must be a list of provenance objects"
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
            raise ClangSignatureError(
                f"audit report compiler {position} has incomplete identity"
            )
        compiler_path = raw_path.strip()
        compiler_id = raw_id.strip()
        if not Path(compiler_path).is_absolute():
            raise ClangSignatureError(
                f"audit report compiler {position} path is not absolute: "
                f"{compiler_path!r}"
        )
        allowed_compilers.add((compiler_path, compiler_id))
        normalized = dict(compiler)
        normalized["compiler_path"] = compiler_path
        normalized["compiler_id"] = compiler_id
        normalized_compilers.append(normalized)
    try:
        normalized_compilers.sort(
            key=lambda row: json.dumps(
                row, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            )
        )
    except (TypeError, ValueError) as e:
        raise ClangSignatureError(
            f"audit report compiler provenance is not JSON-serializable: {e}"
        ) from e

    buckets, validated_counts = _validated_report_rows(report)
    _fail_closed_residuals(buckets)

    matched = buckets["matched"]
    # Fail on any matched row without confirmed location up front.
    unconfirmed = [
        r for r in matched if r.get("line_column_confirmed") is not True
    ]
    if unconfirmed:
        titles = sorted(
            str(r.get("tree_sitter_title") or r.get("name") or "?")
            for r in unconfirmed
        )
        raise ClangSignatureError(
            "clang signature overlay requires line_column_confirmed=true for "
            f"all matched rows; unconfirmed={titles[:10]}"
        )

    by_title = _index_function_entities(data)

    # Deterministic application order.
    matched_sorted = sorted(
        matched,
        key=lambda r: (
            str(r.get("source_path") or ""),
            str(r.get("tree_sitter_title") or ""),
            str(r.get("name") or ""),
        ),
    )

    plans: List[Tuple[Dict[str, Any], Dict[str, Any], str]] = []
    matched_titles: Set[str] = set()
    for row in matched_sorted:
        raw_title = row.get("tree_sitter_title")
        if not isinstance(raw_title, str) or not raw_title:
            raise ClangSignatureError(
                f"matched row missing tree_sitter_title: {row!r}"
            )
        title = raw_title
        if title in matched_titles:
            raise ClangSignatureError(
                f"audit report contains duplicate matched title {title!r}"
            )
        matched_titles.add(title)
        name = row.get("name")
        if (
            not isinstance(name, str)
            or not name
            or title.rsplit(":", 1)[-1] != name
        ):
            raise ClangSignatureError(
                f"matched row title/name mismatch: title={title!r} name={name!r}"
            )
        raw_rel_path = row.get("source_path")
        if not isinstance(raw_rel_path, str) or not raw_rel_path:
            raise ClangSignatureError(
                f"matched row {title!r} missing source_path"
            )
        rel_path = raw_rel_path
        try:
            normalized_rel = package_relative_posix(
                (package_dir / Path(rel_path)).resolve(), package_dir
            )
        except (OSError, ValueError) as e:
            raise ClangSignatureError(
                f"matched row {title!r} has invalid package-relative "
                f"source_path {rel_path!r}: {e}"
            ) from e
        if normalized_rel != rel_path:
            raise ClangSignatureError(
                f"matched row {title!r} has non-canonical source_path "
                f"{rel_path!r}; expected {normalized_rel!r}"
            )

        ents = by_title.get(title) or []
        if len(ents) == 0:
            raise ClangSignatureError(
                f"no function entity for matched title {title!r}"
            )
        if len(ents) > 1:
            raise ClangSignatureError(
                f"non-unique function entity title {title!r} "
                f"({len(ents)} rows); refuse ambiguous attachment"
            )
        entity = ents[0]
        ent_rel = _entity_rel_source(entity, package_dir)
        if ent_rel != rel_path:
            raise ClangSignatureError(
                f"source-path mismatch for {title!r}: graph={ent_rel!r} "
                f"audit={rel_path!r}"
            )

        payload = _signature_payload(
            row,
            expected_digest=digest,
            allowed_compilers=allowed_compilers,
        )
        _validate_payload_against_entity(entity, payload, title=title)
        plans.append((entity, payload, title))

    # A prior overlay from another configuration must not survive silently on
    # a function that is no longer matched (or on a non-function entity).
    for entity in data.get("entities") or []:
        material_fields = [
            key
            for key in _SIGNATURE_FIELDS
            if key in entity
            and entity.get(key) is not None
            and not (
                isinstance(entity.get(key), float)
                and math.isnan(entity.get(key))
            )
        ]
        if not material_fields:
            continue
        title = str(entity.get("title") or "")
        if str(entity.get("type")) != "function":
            raise ClangSignatureError(
                f"non-function entity {title!r} carries Clang signature fields"
            )
        if title not in matched_titles:
            raise ClangSignatureError(
                f"stale Clang signature fields on unmatched entity {title!r}: "
                f"{sorted(material_fields)}"
            )

    # Mutation begins only after every report row, entity, path, provenance,
    # and pre-existing field has passed validation.
    n_changed = 0
    for entity, payload, title in plans:
        if _apply_payload_to_entity(entity, payload, title=title):
            n_changed += 1

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
            key: validated_counts[key]
            for key in (
                "matched",
                "tree_sitter_only",
                "out_of_compile_db_scope",
                "clang_only",
                "ambiguous",
                "macro_location_unsupported",
            )
        },
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }


def append_clang_signatures(
    data: Dict[str, List[Dict[str, Any]]],
    package_dir: Path,
    *,
    timeout: int = 120,
    report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the Clang AST audit (unless ``report`` given) and attach signatures.

    Mutates ``data['entities']`` in place. Returns the manifest provenance
    block for ``extra_manifest['clang_signatures']``.
    """
    package_dir = Path(package_dir).resolve()
    if report is None:
        try:
            report = run_clang_ast_audit(package_dir, timeout=timeout)
        except ClangAstAuditError as e:
            raise ClangSignatureError(str(e)) from e
    return apply_clang_signatures_from_report(data, report, package_dir)
