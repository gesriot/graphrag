#!/usr/bin/env python
"""Pure aggregator for persisted BYOG graph integrity.

Reuses the snapshot-envelope validator and the nine C overlay validators.
Never reads the filesystem, never mutates inputs, and never invokes an
extractor, compiler, or overlay producer.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from graphrag_code.byog_snapshot_integrity import validate_persisted_byog_snapshot  # type: ignore
from graphrag_code.c_clang_calls import validate_persisted_call_overlay  # type: ignore
from graphrag_code.c_clang_signatures import validate_persisted_signature_overlay  # type: ignore
from graphrag_code.c_clang_type_shapes import validate_persisted_type_shape_overlay  # type: ignore
from graphrag_code.c_clang_type_uses import validate_persisted_type_use_overlay  # type: ignore
from graphrag_code.c_clang_types import validate_persisted_type_overlay  # type: ignore
from graphrag_code.c_compiler_facts import validate_persisted_compiler_dependency_overlay  # type: ignore
from graphrag_code.c_compiler_includes import validate_persisted_compiler_include_overlay  # type: ignore
from graphrag_code.c_overlay_coherence import validate_persisted_c_overlay_coherence  # type: ignore
from graphrag_code.c_preprocessor import validate_persisted_preprocessor_liveness  # type: ignore

C_COMPONENT_ORDER: Tuple[str, ...] = (
    "clang_type_use_integrity",
    "clang_type_shape_integrity",
    "clang_type_integrity",
    "clang_signature_integrity",
    "clang_call_integrity",
    "compiler_dependency_integrity",
    "compiler_include_integrity",
    "preprocessor_liveness_integrity",
    "c_overlay_coherence_integrity",
)

C_MANIFEST_BLOCKS: Tuple[str, ...] = (
    "compiler_dependencies",
    "compiler_includes",
    "clang_signatures",
    "clang_calls",
    "clang_types",
    "clang_type_uses",
    "clang_type_shapes",
    "preprocessor_liveness",
)

PYTHON_EXTRACTORS = frozenset(
    {
        "tree-sitter-python",
        "python-ast",
        "tree-sitter-python+ast",
        "advanced-resolver",
    }
)
C_EXTRACTORS = frozenset(
    {
        "tree-sitter-c",
        "c-compiler-deps",
        "c-compiler-includes",
        "clang-ast-json",
    }
)
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
C_SUFFIXES = frozenset({".c", ".h"})

LIMITATIONS = (
    "Persisted snapshot envelope and applicable overlay contracts only",
    "Does not compare the graph with a fresh extractor or current toolchain",
    "Does not repair, reindex, publish, or acquire the publication lock",
    "Does not prove semantic equivalence or source-correctness",
    "A stable staging directory is a publication notice, not proven corruption",
)


class AmbiguousIndexerError(ValueError):
    """Raised when --indexer auto cannot identify one language."""


def _iter_rows(table: Any) -> Iterable[Any]:
    if table is None:
        return
    if isinstance(table, (str, bytes, dict)):
        raise TypeError("table must be a dataframe or sequence of rows")
    if hasattr(table, "iterrows") and not isinstance(table, (list, tuple)):
        for _, row in table.iterrows():
            yield row
        return
    for row in table:
        yield row


def _row_get(row: Any, key: str) -> Any:
    try:
        return row.get(key)
    except Exception:
        try:
            return row[key]
        except Exception:
            return None


def _suffix_language(path: Any) -> Optional[str]:
    if path is None:
        return None
    text = str(path).strip()
    if not text:
        return None
    suffix = Path(text).suffix.lower()
    if suffix in PYTHON_SUFFIXES:
        return "python"
    if suffix in C_SUFFIXES:
        return "c"
    return None


def _extractor_language(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text in PYTHON_EXTRACTORS:
        return "python"
    if text in C_EXTRACTORS:
        return "c"
    return "unknown"


def _collect_language_evidence(
    entities: Any,
    relationships: Any,
    text_units: Any,
    call_observations: Any,
    manifest: Any,
) -> Tuple[List[str], List[str], List[str]]:
    python_votes: List[str] = []
    c_votes: List[str] = []
    unknown: List[str] = []
    for name, table in (
        ("entities", entities),
        ("relationships", relationships),
        ("text_units", text_units),
        ("call_observations", call_observations),
    ):
        for row in _iter_rows(table):
            ext_lang = _suffix_language(_row_get(row, "source_file"))
            if ext_lang == "python":
                python_votes.append(f"{name}.source_file")
            elif ext_lang == "c":
                c_votes.append(f"{name}.source_file")
            extractor_lang = _extractor_language(_row_get(row, "extractor"))
            if extractor_lang == "python":
                python_votes.append(f"{name}.extractor")
            elif extractor_lang == "c":
                c_votes.append(f"{name}.extractor")
            elif extractor_lang == "unknown":
                unknown.append(f"{name}.extractor")
    if isinstance(manifest, Mapping):
        for key in C_MANIFEST_BLOCKS:
            if key in manifest:
                c_votes.append(f"manifest.{key}")
    return python_votes, c_votes, unknown


def resolve_persisted_indexer(
    indexer: str,
    entities: Any,
    relationships: Any,
    text_units: Any,
    call_observations: Any,
    manifest: Any,
) -> Tuple[str, Dict[str, Any]]:
    requested = str(indexer or "").strip().lower()
    if requested in {"python", "c"}:
        return requested, {
            "requested": requested,
            "resolved": requested,
            "reason": "explicit",
        }
    if requested != "auto":
        raise ValueError(f"unknown indexer {indexer!r}; use python, c, or auto")
    python_votes, c_votes, unknown = _collect_language_evidence(
        entities, relationships, text_units, call_observations, manifest
    )
    if unknown or (python_votes and c_votes) or (not python_votes and not c_votes):
        raise AmbiguousIndexerError(
            "auto-indexer is ambiguous; pass --indexer python or --indexer c"
        )
    if python_votes:
        resolved = "python"
    else:
        resolved = "c"
    return resolved, {
        "requested": "auto",
        "resolved": resolved,
        "reason": "persisted source_file extensions and extractor provenance",
        "n_python_votes": len(python_votes),
        "n_c_votes": len(c_votes),
    }


def _run_c_component(
    name: str,
    entities: Any,
    relationships: Any,
    call_observations: Any,
    manifest: Any,
    *,
    max_anomaly_samples: int,
) -> Dict[str, Any]:
    runners: Dict[str, Callable[[], Dict[str, Any]]] = {
        "clang_type_use_integrity": lambda: validate_persisted_type_use_overlay(
            entities, relationships, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "clang_type_shape_integrity": lambda: validate_persisted_type_shape_overlay(
            entities, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "clang_type_integrity": lambda: validate_persisted_type_overlay(
            entities, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "clang_signature_integrity": lambda: validate_persisted_signature_overlay(
            entities, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "clang_call_integrity": lambda: validate_persisted_call_overlay(
            relationships, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "compiler_dependency_integrity": lambda: validate_persisted_compiler_dependency_overlay(
            entities, relationships, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "compiler_include_integrity": lambda: validate_persisted_compiler_include_overlay(
            entities, relationships, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "preprocessor_liveness_integrity": lambda: validate_persisted_preprocessor_liveness(
            entities,
            relationships,
            call_observations,
            manifest,
            max_anomaly_samples=max_anomaly_samples,
        ),
        "c_overlay_coherence_integrity": lambda: validate_persisted_c_overlay_coherence(
            entities,
            relationships,
            call_observations,
            manifest,
            max_anomaly_samples=max_anomaly_samples,
        ),
    }
    return runners[name]()


def _tag_anomalies(
    component: str, anomalies: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    tagged: List[Dict[str, Any]] = []
    for item in anomalies:
        row = dict(item)
        nested_component = row.get("component")
        if nested_component is not None and nested_component != component:
            row.setdefault("subcomponent", nested_component)
        row["component"] = component
        tagged.append(row)
    return tagged


def validate_persisted_graph_integrity(
    entities: Any,
    relationships: Any,
    text_units: Any,
    call_observations: Any,
    manifest: Any,
    *,
    indexer: str,
    snapshot_id: Optional[str] = None,
    present_files: Optional[Iterable[Any]] = None,
    file_sizes: Optional[Mapping[Any, Any]] = None,
    symlinked_files: Optional[Iterable[Any]] = None,
    unexpected_entries: Optional[Iterable[Any]] = None,
    max_anomaly_samples: int = 40,
) -> Dict[str, Any]:
    """Validate the persisted envelope and every applicable overlay contract.

    Pure and non-mutating. ``indexer='auto'`` uses persisted extensions and
    extractor provenance only when that signal is complete and unambiguous.
    """
    if max_anomaly_samples < 0:
        max_anomaly_samples = 0

    requested = str(indexer or "").strip().lower()
    if requested not in {"python", "c", "auto"}:
        raise ValueError(f"unknown indexer {indexer!r}; use python, c, or auto")

    snapshot = validate_persisted_byog_snapshot(
        entities,
        relationships,
        text_units,
        call_observations,
        manifest,
        snapshot_id=snapshot_id,
        present_files=present_files,
        file_sizes=file_sizes,
        max_anomaly_samples=max_anomaly_samples,
        symlinked_files=symlinked_files,
        unexpected_entries=unexpected_entries,
    )

    envelope_ok = bool(snapshot.get("ok"))
    if requested in {"python", "c"}:
        resolved = requested
        resolution = {"requested": requested, "resolved": requested, "reason": "explicit"}
    elif envelope_ok:
        resolved, resolution = resolve_persisted_indexer(
            indexer, entities, relationships, text_units, call_observations, manifest
        )
    else:
        resolved = requested if requested in {"python", "c"} else "unresolved"
        resolution = {
            "requested": requested or "auto",
            "resolved": resolved,
            "reason": "envelope_invalid",
        }

    components: Dict[str, Dict[str, Any]] = {}
    failed: List[str] = []
    if not snapshot["ok"]:
        failed.append("snapshot_integrity")
    elif resolved == "c":
        for name in C_COMPONENT_ORDER:
            result = _run_c_component(
                name,
                entities,
                relationships,
                call_observations,
                manifest,
                max_anomaly_samples=max_anomaly_samples,
            )
            components[name] = result
            if not result.get("ok"):
                failed.append(name)

    n_anomalies = int(snapshot.get("n_anomalies") or 0)
    for result in components.values():
        n_anomalies += int(result.get("n_anomalies") or 0)

    samples: List[Dict[str, Any]] = []
    samples.extend(_tag_anomalies("snapshot_integrity", snapshot.get("anomalies") or []))
    for name in C_COMPONENT_ORDER:
        result = components.get(name)
        if result is None:
            continue
        samples.extend(_tag_anomalies(name, result.get("anomalies") or []))
    samples = samples[:max_anomaly_samples]

    ok = not failed
    status = "valid" if ok else "invalid"
    return {
        "ok": ok,
        "status": status,
        "state": status,
        "classification": status,
        "indexer": resolved if resolved in {"python", "c"} else requested,
        "indexer_resolution": resolution,
        "snapshot_integrity": snapshot,
        "components": components,
        "failed_components": failed,
        "n_anomalies": n_anomalies,
        "n_anomaly_samples": len(samples),
        "anomalies_truncated": n_anomalies > len(samples),
        "anomalies": samples,
        "limitations": list(LIMITATIONS),
    }
