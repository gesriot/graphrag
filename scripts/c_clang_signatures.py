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

# Audit-report contract text only; not a persisted manifest field.
LIMITATIONS = (
    "Configured function-signature confirmation of existing function entities only",
    "Not a new entity or relationship layer",
    "Not full type resolution",
    "Not ABI verification",
    "Not multi-config coverage",
    "tree_sitter_only and out_of_compile_db_scope invent no signatures",
)

# Count keys persisted in the enabled clang_signatures manifest block.
_MANIFEST_COUNT_KEYS = (
    "matched",
    "tree_sitter_only",
    "out_of_compile_db_scope",
    "clang_only",
    "ambiguous",
    "macro_location_unsupported",
)
_FAIL_CLOSED_COUNT_KEYS = (
    "clang_only",
    "ambiguous",
    "macro_location_unsupported",
)
_OBSERVATION_COUNT_KEYS = (
    "tree_sitter_only",
    "out_of_compile_db_scope",
)

_OFF_MANIFEST_FIELDS = frozenset(
    {
        "mode",
        "enabled",
        "n_facts",
        "n_translation_units",
    }
)
_ENABLED_MANIFEST_FIELDS = frozenset(
    {
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
        "confidence_boundary",
    }
)

# Known producer fields that do not use the clang_signature_* prefix.
_NON_PREFIXED_SIGNATURE_FIELDS = (
    "clang_qual_type",
    "clang_storage_class",
    "clang_inline",
    "clang_variadic",
    "clang_mangled_name",
    "clang_location_origin",
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
        if key not in entity or entity.get(key) != new_val:
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
        "stale_signature_metadata",
        "partial_signature_payload",
        "unknown_signature_field",
        "signature_field_type",
        "identity_mismatch",
        "description_mismatch",
        "observations_json",
        "observation_record",
        "observation_qual_type",
        "observation_order",
        "entry_index_census",
        "compiler_mismatch",
        "digest_mismatch",
        "manifest_mode_mismatch",
        "manifest_identity_mismatch",
        "manifest_count_mismatch",
        "manifest_contract_claim",
        "residual_bucket_nonzero",
    }
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
    entity_id: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if code not in ANOMALY_CODES:
        raise AssertionError(f"unknown signature integrity anomaly code {code!r}")
    row: Dict[str, Any] = {"code": code, "message": _clip(message)}
    if entity_id is not None:
        row["entity_id"] = entity_id
    if extra:
        for key, value in sorted(extra.items()):
            row[key] = _clip(value) if isinstance(value, str) else value
    return row


def _is_signature_field(key: str) -> bool:
    return key.startswith("clang_signature_") or key in _NON_PREFIXED_SIGNATURE_FIELDS


def _has_material_signature_fields(entity: Any) -> bool:
    for key in _row_keys(entity):
        if _is_signature_field(key) and is_material_value(_row_get(entity, key)):
            return True
    return False


def _producer_description(title: str, qual: str) -> str:
    return (
        f"configured Clang function signature for {title}: {qual} "
        f"(fact_kind={FACT_KIND}; deterministic only relative to recorded "
        f"Clang + compile_commands.json)"
    )


def _decoded_indices(
    raw: Any,
    *,
    entity_id: Optional[str],
    field: str,
    n_compile_entries: Optional[int],
    anomalies: List[Dict[str, Any]],
    code: str = "entry_index_census",
) -> Optional[List[int]]:
    values = _normalize_list_field(raw)
    if values is None or not values:
        anomalies.append(
            _anomaly(
                code,
                f"{field} is not a non-empty list",
                entity_id=entity_id,
            )
        )
        return None
    decoded = [_as_int(index) for index in values]
    if any(index is None or index < 0 for index in decoded):
        anomalies.append(
            _anomaly(
                code,
                f"{field} has non-integer or negative entries: {values!r}",
                entity_id=entity_id,
            )
        )
        return None
    ints = [int(index) for index in decoded]  # type: ignore[arg-type]
    if ints != sorted(set(ints)):
        anomalies.append(
            _anomaly(
                code,
                f"{field} is not sorted/unique: {ints!r}",
                entity_id=entity_id,
            )
        )
        return None
    if n_compile_entries is not None and any(
        index >= n_compile_entries for index in ints
    ):
        anomalies.append(
            _anomaly(
                code,
                f"{field} {ints!r} outside manifest compile-entry census "
                f"(n={n_compile_entries})",
                entity_id=entity_id,
            )
        )
        return None
    return ints


def _validate_observations(
    raw: Any,
    *,
    entity_id: Optional[str],
    entity_qual: Any,
    entity_digest: Optional[str],
    entity_indices: Optional[List[int]],
    manifest_digest: Optional[str],
    manifest_compilers: Set[Tuple[str, str]],
    n_compile_entries: Optional[int],
    anomalies: List[Dict[str, Any]],
) -> None:
    if not isinstance(raw, str) or not raw:
        anomalies.append(
            _anomaly(
                "observations_json",
                "clang_signature_observations_json is not a non-empty JSON "
                f"string: {type(raw).__name__}",
                entity_id=entity_id,
            )
        )
        return
    try:
        decoded = strict_json_loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        anomalies.append(
            _anomaly(
                "observations_json",
                f"clang_signature_observations_json is not strict JSON: {error}",
                entity_id=entity_id,
            )
        )
        return
    if _contains_non_finite(decoded):
        anomalies.append(
            _anomaly(
                "observations_json",
                "clang_signature_observations_json contains NaN/Infinity",
                entity_id=entity_id,
            )
        )
        return
    try:
        canonical = _strict_canonical_json(decoded)
    except (TypeError, ValueError) as error:
        anomalies.append(
            _anomaly(
                "observations_json",
                f"clang_signature_observations_json is not canonical-encodable: "
                f"{error}",
                entity_id=entity_id,
            )
        )
        return
    if canonical != raw:
        anomalies.append(
            _anomaly(
                "observations_json",
                "clang_signature_observations_json is not producer-canonical JSON",
                entity_id=entity_id,
            )
        )
        return
    if not isinstance(decoded, list) or not decoded:
        anomalies.append(
            _anomaly(
                "observations_json",
                "clang_signature_observations_json must be a non-empty list",
                entity_id=entity_id,
            )
        )
        return
    if not all(isinstance(row, dict) for row in decoded):
        anomalies.append(
            _anomaly(
                "observation_record",
                "clang_signature_observations_json must contain objects",
                entity_id=entity_id,
            )
        )
        return
    expected_order = sorted(decoded, key=_strict_canonical_json)
    if decoded != expected_order:
        anomalies.append(
            _anomaly(
                "observation_order",
                "clang_signature_observations_json is not in producer "
                "canonical order",
                entity_id=entity_id,
            )
        )

    observed: Set[int] = set()
    required = (
        "entry_indices",
        "compiler_path",
        "compiler_id",
        "compile_commands_digest",
        "qualType",
    )
    for position, record in enumerate(decoded):
        missing = [field for field in required if field not in record]
        if missing:
            anomalies.append(
                _anomaly(
                    "observation_record",
                    f"observation[{position}] missing {missing}",
                    entity_id=entity_id,
                )
            )
            continue
        indices = _decoded_indices(
            record.get("entry_indices"),
            entity_id=entity_id,
            field=f"observation[{position}].entry_indices",
            n_compile_entries=n_compile_entries,
            anomalies=anomalies,
            code="observation_record",
        )
        if indices is not None:
            observed.update(indices)
        path = record.get("compiler_path")
        cid = record.get("compiler_id")
        digest = record.get("compile_commands_digest")
        qual = record.get("qualType")
        if not isinstance(path, str) or not path.strip():
            anomalies.append(
                _anomaly(
                    "observation_record",
                    f"observation[{position}] has empty compiler_path",
                    entity_id=entity_id,
                )
            )
        elif path != path.strip() or not Path(path).is_absolute():
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    f"observation[{position}] compiler_path is not a canonical "
                    "absolute path: "
                    f"{path!r}",
                    entity_id=entity_id,
                )
            )
        if not isinstance(cid, str) or not cid.strip():
            anomalies.append(
                _anomaly(
                    "observation_record",
                    f"observation[{position}] has empty compiler_id",
                    entity_id=entity_id,
                )
            )
        elif cid != cid.strip():
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    f"observation[{position}] compiler_id is not canonical: "
                    f"{cid!r}",
                    entity_id=entity_id,
                )
            )
        if (
            isinstance(path, str)
            and path
            and isinstance(cid, str)
            and cid
            and manifest_compilers
            and (path, cid) not in manifest_compilers
        ):
            anomalies.append(
                _anomaly(
                    "compiler_mismatch",
                    f"observation[{position}] compiler identity absent from "
                    "manifest census",
                    entity_id=entity_id,
                )
            )
        if not isinstance(digest, str) or not digest.strip():
            anomalies.append(
                _anomaly(
                    "observation_record",
                    f"observation[{position}] has empty compile_commands_digest",
                    entity_id=entity_id,
                )
            )
        elif digest != digest.strip():
            anomalies.append(
                _anomaly(
                    "digest_mismatch",
                    f"observation[{position}] digest is not canonical: "
                    f"{digest!r}",
                    entity_id=entity_id,
                )
            )
        else:
            expected = entity_digest or manifest_digest
            if expected is not None and digest != expected:
                anomalies.append(
                    _anomaly(
                        "digest_mismatch",
                        f"observation[{position}] digest {digest!r} "
                        f"!= expected {expected!r}",
                        entity_id=entity_id,
                    )
                )
        if not isinstance(qual, str) or not qual.strip():
            anomalies.append(
                _anomaly(
                    "observation_record",
                    f"observation[{position}] has empty qualType",
                    entity_id=entity_id,
                )
            )
        elif isinstance(entity_qual, str) and qual.strip() != entity_qual:
            anomalies.append(
                _anomaly(
                    "observation_qual_type",
                    f"observation[{position}] qualType {qual!r} != entity "
                    f"{entity_qual!r}",
                    entity_id=entity_id,
                )
            )
    if entity_indices is not None and observed != set(entity_indices):
        anomalies.append(
            _anomaly(
                "entry_index_census",
                f"observation entry-index union {sorted(observed)!r} != "
                f"clang_signature_entry_indices {entity_indices!r}",
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
) -> None:
    present_keys = set(_row_keys(entity))
    missing = [field for field in _SIGNATURE_FIELDS if field not in present_keys]
    if missing:
        anomalies.append(
            _anomaly(
                "partial_signature_payload",
                f"decorated entity is missing required keys: {missing}",
                entity_id=entity_id,
            )
        )
    unknown = sorted(
        key
        for key in present_keys
        if key.startswith("clang_signature_")
        and key not in _SIGNATURE_FIELDS
        and is_material_value(_row_get(entity, key))
    )
    if unknown:
        anomalies.append(
            _anomaly(
                "unknown_signature_field",
                f"unknown clang_signature_* fields: {unknown}",
                entity_id=entity_id,
            )
        )

    if _row_get(entity, "clang_signature_status") != "matched":
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_signature_status="
                f"{_row_get(entity, 'clang_signature_status')!r} expected "
                "'matched'",
                entity_id=entity_id,
            )
        )
    if _row_get(entity, "clang_signature_fact_kind") != FACT_KIND:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_signature_fact_kind="
                f"{_row_get(entity, 'clang_signature_fact_kind')!r} expected "
                f"{FACT_KIND!r}",
                entity_id=entity_id,
            )
        )
    if _row_get(entity, "clang_signature_extractor") != EXTRACTOR:
        anomalies.append(
            _anomaly(
                "identity_mismatch",
                f"clang_signature_extractor="
                f"{_row_get(entity, 'clang_signature_extractor')!r} expected "
                f"{EXTRACTOR!r}",
                entity_id=entity_id,
            )
        )
    if not _is_one(_row_get(entity, "clang_signature_confidence")):
        anomalies.append(
            _anomaly(
                "signature_field_type",
                f"clang_signature_confidence="
                f"{_row_get(entity, 'clang_signature_confidence')!r} "
                "expected 1.0",
                entity_id=entity_id,
            )
        )
    if _as_bool(_row_get(entity, "clang_signature_is_deterministic")) is not True:
        anomalies.append(
            _anomaly(
                "signature_field_type",
                "clang_signature_is_deterministic is not boolean true",
                entity_id=entity_id,
            )
        )

    qual = _row_get(entity, "clang_qual_type")
    if not isinstance(qual, str) or not qual.strip():
        anomalies.append(
            _anomaly(
                "signature_field_type",
                f"clang_qual_type is not a non-empty string: {qual!r}",
                entity_id=entity_id,
            )
        )
        qual = None
    storage = _row_get(entity, "clang_storage_class")
    if is_material_value(storage) and not isinstance(storage, str):
        anomalies.append(
            _anomaly(
                "signature_field_type",
                f"clang_storage_class must be a string or null: {storage!r}",
                entity_id=entity_id,
            )
        )
    inline = _row_get(entity, "clang_inline")
    if is_material_value(inline) and _as_bool(inline) is None:
        anomalies.append(
            _anomaly(
                "signature_field_type",
                f"clang_inline must be a boolean or null: {inline!r}",
                entity_id=entity_id,
            )
        )
    variadic = _row_get(entity, "clang_variadic")
    if is_material_value(variadic) and _as_bool(variadic) is None:
        anomalies.append(
            _anomaly(
                "signature_field_type",
                f"clang_variadic must be a boolean or null: {variadic!r}",
                entity_id=entity_id,
            )
        )
    mangled = _row_get(entity, "clang_mangled_name")
    if is_material_value(mangled) and not isinstance(mangled, str):
        anomalies.append(
            _anomaly(
                "signature_field_type",
                f"clang_mangled_name must be a string or null: {mangled!r}",
                entity_id=entity_id,
            )
        )
    origin = _row_get(entity, "clang_location_origin")
    if not isinstance(origin, str) or not origin.strip():
        anomalies.append(
            _anomaly(
                "signature_field_type",
                "clang_location_origin is not a non-empty string",
                entity_id=entity_id,
            )
        )

    compiler_path = _row_get(entity, "clang_signature_compiler_path")
    compiler_id = _row_get(entity, "clang_signature_compiler_id")
    if (
        not isinstance(compiler_path, str)
        or not compiler_path.strip()
        or compiler_path != compiler_path.strip()
        or not Path(compiler_path).is_absolute()
    ):
        anomalies.append(
            _anomaly(
                "compiler_mismatch",
                f"clang_signature_compiler_path is not an absolute path: "
                f"{compiler_path!r}",
                entity_id=entity_id,
            )
        )
        compiler_path = None
    if (
        not isinstance(compiler_id, str)
        or not compiler_id.strip()
        or compiler_id != compiler_id.strip()
    ):
        anomalies.append(
            _anomaly(
                "compiler_mismatch",
                f"clang_signature_compiler_id is empty: {compiler_id!r}",
                entity_id=entity_id,
            )
        )
        compiler_id = None
    if (
        compiler_path is not None
        and compiler_id is not None
        and manifest_compilers
        and (compiler_path, compiler_id) not in manifest_compilers
    ):
        anomalies.append(
            _anomaly(
                "compiler_mismatch",
                "entity compiler identity is absent from the manifest census",
                entity_id=entity_id,
            )
        )

    digest = _row_get(entity, "clang_signature_compile_commands_digest")
    if not isinstance(digest, str) or not digest.strip():
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"clang_signature_compile_commands_digest is empty: {digest!r}",
                entity_id=entity_id,
            )
        )
        digest = None
    elif digest != digest.strip():
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"clang_signature_compile_commands_digest is not canonical: "
                f"{digest!r}",
                entity_id=entity_id,
            )
        )
        digest = None
    else:
        if manifest_digest is not None and digest != manifest_digest:
            anomalies.append(
                _anomaly(
                    "digest_mismatch",
                    f"entity digest {digest!r} != manifest {manifest_digest!r}",
                    entity_id=entity_id,
                )
            )

    entry_indices = _decoded_indices(
        _row_get(entity, "clang_signature_entry_indices"),
        entity_id=entity_id,
        field="clang_signature_entry_indices",
        n_compile_entries=n_compile_entries,
        anomalies=anomalies,
    )
    _validate_observations(
        _row_get(entity, "clang_signature_observations_json"),
        entity_id=entity_id,
        entity_qual=qual,
        entity_digest=digest,
        entity_indices=entry_indices,
        manifest_digest=manifest_digest,
        manifest_compilers=manifest_compilers,
        n_compile_entries=n_compile_entries,
        anomalies=anomalies,
    )

    description = _row_get(entity, "clang_signature_description")
    title = _row_get(entity, "title")
    if not isinstance(description, str) or not description:
        anomalies.append(
            _anomaly(
                "description_mismatch",
                "clang_signature_description is empty",
                entity_id=entity_id,
            )
        )
    elif isinstance(title, str) and isinstance(qual, str):
        expected = _producer_description(title, qual)
        if description != expected:
            anomalies.append(
                _anomaly(
                    "description_mismatch",
                    "clang_signature_description does not match the producer "
                    "format",
                    entity_id=entity_id,
                )
            )


def _validate_signature_manifest_block(
    block: Dict[str, Any],
    *,
    n_decorated: int,
    n_entities: int,
    manifest_obj: Dict[str, Any],
    anomalies: List[Dict[str, Any]],
) -> Tuple[Optional[str], Set[Tuple[str, str]], Optional[int], Dict[str, int]]:
    block_keys = set(block)
    missing_block_keys = sorted(_ENABLED_MANIFEST_FIELDS - block_keys)
    unknown_block_keys = sorted(
        repr(key) for key in block_keys - _ENABLED_MANIFEST_FIELDS
    )
    if missing_block_keys or unknown_block_keys:
        anomalies.append(
            _anomaly(
                "manifest_contract_claim",
                "enabled manifest key set differs from the producer contract: "
                f"missing={missing_block_keys}, unknown={unknown_block_keys}",
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
        counts_keys = set(counts_raw)
        missing_count_keys = sorted(set(_MANIFEST_COUNT_KEYS) - counts_keys)
        unknown_count_keys = sorted(
            repr(key) for key in counts_keys - set(_MANIFEST_COUNT_KEYS)
        )
        if missing_count_keys or unknown_count_keys:
            anomalies.append(
                _anomaly(
                    "manifest_count_mismatch",
                    "manifest counts key set differs from the producer "
                    f"contract: missing={missing_count_keys}, "
                    f"unknown={unknown_count_keys}",
                )
            )
        for key in _MANIFEST_COUNT_KEYS:
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

    raw_digest = block.get("compile_commands_digest")
    digest: Optional[str] = None
    if not isinstance(raw_digest, str) or not raw_digest.strip():
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"manifest compile_commands_digest missing: {raw_digest!r}",
            )
        )
    elif raw_digest != raw_digest.strip():
        anomalies.append(
            _anomaly(
                "digest_mismatch",
                f"manifest compile_commands_digest is not canonical: "
                f"{raw_digest!r}",
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
                or path != path.strip()
                or not Path(path).is_absolute()
                or not isinstance(cid, str)
                or not cid.strip()
                or cid != cid.strip()
            ):
                anomalies.append(
                    _anomaly(
                        "manifest_identity_mismatch",
                        f"manifest compilers[{position}] has incomplete identity",
                    )
                )
                continue
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


def validate_persisted_signature_overlay(
    entities: Any,
    manifest: Optional[Any] = None,
    *,
    max_anomaly_samples: int = _MAX_ANOMALY_SAMPLES,
) -> Dict[str, Any]:
    """Validate already-persisted Clang function-signature entity evidence.

    Pure and non-mutating. Never invokes Clang, loads compile_commands.json,
    reindexes, or rewrites graphs.

    Compatibility states:
      * no ``clang_signatures`` block and zero signature fields ->
        ``legacy_absent``
      * ``mode=off`` / ``enabled=false`` -> requires zero signature fields
      * enabled ``clang_ast_signatures`` -> full entity + manifest census

    A missing manifest block never legitimizes existing signature fields.
    A present ``clang_signatures`` key that is null/list/string is invalid,
    not legacy. Nothing is ever repaired.
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
        if not _has_material_signature_fields(entity):
            continue
        carrying.append(entity)
        entity_id = str(_row_get(entity, "id") or "") or None
        if str(_row_get(entity, "type") or "") == "function":
            decorated.append(entity)
        else:
            anomalies.append(
                _anomaly(
                    "stale_signature_metadata",
                    f"non-function entity carries Clang signature fields "
                    f"(type={_row_get(entity, 'type')!r})",
                    entity_id=entity_id,
                )
            )

    has_block = "clang_signatures" in manifest_obj
    block = manifest_obj.get("clang_signatures")
    mode_state = "legacy_absent"
    block_enabled = False

    if not has_block:
        if carrying:
            anomalies.append(
                _anomaly(
                    "legacy_block_missing_with_fields",
                    f"manifest lacks clang_signatures but graph has "
                    f"{len(carrying)} entity/entities with signature fields",
                    extra={"n_entities": len(carrying)},
                )
            )
            mode_state = "invalid"
    elif not isinstance(block, dict):
        anomalies.append(
            _anomaly(
                "invalid_enabled_block",
                f"clang_signatures manifest block is not an object: "
                f"{type(block).__name__}",
            )
        )
        mode_state = "invalid"
    else:
        mode = block.get("mode")
        enabled = block.get("enabled")
        if mode == "off" and enabled is False:
            mode_state = "off"
            block_keys = set(block)
            missing_block_keys = sorted(_OFF_MANIFEST_FIELDS - block_keys)
            unknown_block_keys = sorted(
                repr(key) for key in block_keys - _OFF_MANIFEST_FIELDS
            )
            if missing_block_keys or unknown_block_keys:
                anomalies.append(
                    _anomaly(
                        "manifest_contract_claim",
                        "off manifest key set differs from the producer "
                        f"contract: missing={missing_block_keys}, "
                        f"unknown={unknown_block_keys}",
                    )
                )
            if carrying:
                anomalies.append(
                    _anomaly(
                        "off_with_decorated_entities",
                        f"clang_signatures is off/disabled but graph has "
                        f"{len(carrying)} entity/entities with signature fields",
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
                    f"clang_signatures enablement inconsistent: mode={mode!r} "
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
        ) = _validate_signature_manifest_block(
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
        "n_signature_field_carriers": len(carrying),
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
