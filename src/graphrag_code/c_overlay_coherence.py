#!/usr/bin/env python
"""Read-only snapshot-wide coherence for persisted C compiler/Clang overlays.

Each component validator proves one overlay block and its rows are internally
valid. This module additionally proves that every *enabled* compiler-backed
overlay in one snapshot shares the same compile-database digest, compile-entry
census, and normalized compiler identity.

It never invokes a compiler, reads sources or compile_commands.json,
reconstructs overlay facts, compares provenance with the current host, or
repairs persisted data.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from graphrag_code.c_clang_calls import validate_persisted_call_overlay  # type: ignore
from graphrag_code.c_clang_signatures import validate_persisted_signature_overlay  # type: ignore
from graphrag_code.c_clang_type_shapes import validate_persisted_type_shape_overlay  # type: ignore
from graphrag_code.c_clang_type_uses import validate_persisted_type_use_overlay  # type: ignore
from graphrag_code.c_clang_types import validate_persisted_type_overlay  # type: ignore
from graphrag_code.c_compiler_facts import (  # type: ignore
    validate_persisted_compiler_dependency_overlay,
)
from graphrag_code.c_compiler_includes import (  # type: ignore
    validate_persisted_compiler_include_overlay,
)
from graphrag_code.c_preprocessor import validate_persisted_preprocessor_liveness  # type: ignore

_MAX_ANOMALY_SAMPLES = 40
_MAX_ANOMALY_MESSAGE = 400

COMPILER_BACKED_COMPONENTS: Tuple[str, ...] = (
    "compiler_dependencies",
    "compiler_includes",
    "clang_signatures",
    "clang_calls",
    "clang_types",
    "clang_type_uses",
    "clang_type_shapes",
)
TU_COMPARE_COMPONENTS = ("compiler_dependencies", "compiler_includes")
LIVENESS_COMPONENT = "preprocessor_liveness"

LIMITATIONS = (
    "Persisted configuration agreement across enabled compiler-backed overlays only",
    "Does not prove overlay facts against live sources or a current toolchain",
    "Does not compare preprocessor_liveness with compiler-overlay identities",
    "Off and legacy-absent blocks do not participate in equality comparisons",
    "Does not require every overlay flag to be enabled together",
)

ANOMALY_CODES = frozenset(
    {
        "component_integrity",
        "digest_mismatch",
        "compile_entry_count_mismatch",
        "compiler_census_mismatch",
        "compiler_shortcut_mismatch",
        "translation_unit_census_mismatch",
    }
)

_ComponentFn = Callable[..., Dict[str, Any]]


def _strict_canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _clip(text: Any, limit: int = _MAX_ANOMALY_MESSAGE) -> str:
    s = str(text)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _anomaly(
    code: str,
    message: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if code not in ANOMALY_CODES:
        raise AssertionError(f"unknown overlay-coherence anomaly code {code!r}")
    row: Dict[str, Any] = {"code": code, "message": _clip(message)}
    if extra:
        for key, value in sorted(extra.items(), key=lambda item: repr(item[0])):
            row[key] = _clip(value) if isinstance(value, str) else value
    return row


def _as_mapping(manifest: Optional[Any]) -> Dict[str, Any]:
    if manifest is None:
        return {}
    if isinstance(manifest, dict):
        return manifest
    if hasattr(manifest, "items"):
        return dict(manifest)
    raise TypeError("manifest must be a mapping or None")


def _summarize_component(result: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "ok": bool(result.get("ok")),
        "status": str(result.get("status")),
        "mode": str(result.get("mode")),
        "n_anomalies": int(result.get("n_anomalies") or 0),
        "n_anomaly_samples": int(result.get("n_anomaly_samples") or 0),
        "anomalies_truncated": bool(result.get("anomalies_truncated")),
        "anomalies": list(result.get("anomalies") or []),
    }


def _run_components(
    entities: Any,
    relationships: Any,
    call_observations: Any,
    manifest: Dict[str, Any],
    *,
    max_anomaly_samples: int,
) -> Dict[str, Dict[str, Any]]:
    runners: Dict[str, Callable[[], Dict[str, Any]]] = {
        "compiler_dependencies": lambda: validate_persisted_compiler_dependency_overlay(
            entities, relationships, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "compiler_includes": lambda: validate_persisted_compiler_include_overlay(
            entities, relationships, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "clang_signatures": lambda: validate_persisted_signature_overlay(
            entities, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "clang_calls": lambda: validate_persisted_call_overlay(
            relationships, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "clang_types": lambda: validate_persisted_type_overlay(
            entities, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "clang_type_uses": lambda: validate_persisted_type_use_overlay(
            entities, relationships, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        "clang_type_shapes": lambda: validate_persisted_type_shape_overlay(
            entities, manifest, max_anomaly_samples=max_anomaly_samples
        ),
        LIVENESS_COMPONENT: lambda: validate_persisted_preprocessor_liveness(
            entities,
            relationships,
            call_observations,
            manifest,
            max_anomaly_samples=max_anomaly_samples,
        ),
    }
    return {name: _summarize_component(fn()) for name, fn in runners.items()}


def _normalize_compiler_record(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    return {
        "compiler_path": item.get("compiler_path"),
        "compiler_id": item.get("compiler_id"),
        "compiler_version": item.get("compiler_version"),
    }


def _compiler_sort_key(record: Mapping[str, Any]) -> Tuple[str, str, str]:
    return (
        str(record.get("compiler_path") or ""),
        str(record.get("compiler_id") or ""),
        str(record.get("compiler_version") or ""),
    )


def _extract_shared(name: str, block: Mapping[str, Any]) -> Dict[str, Any]:
    raw_compilers = block.get("compilers")
    compilers: List[Dict[str, Any]] = []
    if isinstance(raw_compilers, list):
        for item in raw_compilers:
            normalized = _normalize_compiler_record(item)
            if normalized is not None:
                compilers.append(normalized)
    compilers.sort(key=_compiler_sort_key)
    shared: Dict[str, Any] = {
        "component": name,
        "compile_commands_digest": block.get("compile_commands_digest"),
        "n_compile_entries": block.get("n_compile_entries"),
        "compilers": compilers,
        "compiler_path": block.get("compiler_path"),
        "compiler_id": block.get("compiler_id"),
        "compiler_version": block.get("compiler_version"),
    }
    if name in TU_COMPARE_COMPONENTS:
        shared["n_translation_units"] = block.get("n_translation_units")
        shared["translation_unit_titles"] = block.get("translation_unit_titles")
    return shared


def _compare_enabled(
    enabled: Sequence[str],
    manifest: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    snapshots = [_extract_shared(name, manifest[name]) for name in enabled]
    anomalies: List[Dict[str, Any]] = []
    if len(snapshots) < 2:
        return anomalies

    first = snapshots[0]
    for other in snapshots[1:]:
        pair = [first["component"], other["component"]]
        if first["compile_commands_digest"] != other["compile_commands_digest"]:
            anomalies.append(
                _anomaly(
                    "digest_mismatch",
                    f"{pair[0]} compile_commands_digest="
                    f"{first['compile_commands_digest']!r} != {pair[1]} "
                    f"{other['compile_commands_digest']!r}",
                    extra={"components": pair},
                )
            )
        if first["n_compile_entries"] != other["n_compile_entries"]:
            anomalies.append(
                _anomaly(
                    "compile_entry_count_mismatch",
                    f"{pair[0]} n_compile_entries="
                    f"{first['n_compile_entries']!r} != {pair[1]} "
                    f"{other['n_compile_entries']!r}",
                    extra={"components": pair},
                )
            )
        if first["compilers"] != other["compilers"]:
            anomalies.append(
                _anomaly(
                    "compiler_census_mismatch",
                    f"{pair[0]} compiler census disagrees with {pair[1]}",
                    extra={"components": pair},
                )
            )
        for field in ("compiler_path", "compiler_id", "compiler_version"):
            if first[field] != other[field]:
                anomalies.append(
                    _anomaly(
                        "compiler_shortcut_mismatch",
                        f"{pair[0]} {field}={first[field]!r} != {pair[1]} "
                        f"{other[field]!r}",
                        extra={"components": pair, "field": field},
                    )
                )

    dep_inc = [item for item in snapshots if item["component"] in TU_COMPARE_COMPONENTS]
    if len(dep_inc) == 2:
        left, right = dep_inc
        pair = [left["component"], right["component"]]
        if left["n_translation_units"] != right["n_translation_units"]:
            anomalies.append(
                _anomaly(
                    "translation_unit_census_mismatch",
                    f"{pair[0]} n_translation_units="
                    f"{left['n_translation_units']!r} != {pair[1]} "
                    f"{right['n_translation_units']!r}",
                    extra={"components": pair},
                )
            )
        if left["translation_unit_titles"] != right["translation_unit_titles"]:
            anomalies.append(
                _anomaly(
                    "translation_unit_census_mismatch",
                    f"{pair[0]} translation_unit_titles disagree with {pair[1]}",
                    extra={"components": pair},
                )
            )
    return anomalies


def validate_persisted_c_overlay_coherence(
    entities: Any,
    relationships: Any,
    call_observations: Any = None,
    manifest: Optional[Any] = None,
    *,
    max_anomaly_samples: int = _MAX_ANOMALY_SAMPLES,
) -> Dict[str, Any]:
    """Validate snapshot-wide compiler-overlay configuration agreement.

    Pure and non-mutating. Component row contracts stay in their existing
    validators; this function only compares shared configuration among
    enabled compiler-backed overlays.
    """
    if max_anomaly_samples < 0:
        max_anomaly_samples = 0

    manifest_obj = _as_mapping(manifest)
    components = _run_components(
        entities,
        relationships,
        call_observations,
        manifest_obj,
        max_anomaly_samples=max_anomaly_samples,
    )

    absent: List[str] = []
    off: List[str] = []
    enabled: List[str] = []
    failed: List[str] = []
    for name in COMPILER_BACKED_COMPONENTS:
        result = components[name]
        if not result["ok"]:
            failed.append(name)
            continue
        if result["mode"] == "legacy_absent":
            absent.append(name)
        elif result["mode"] == "off":
            off.append(name)
        elif result["mode"] == "enabled":
            enabled.append(name)
        else:
            failed.append(name)

    component_failures = list(failed)
    if not components[LIVENESS_COMPONENT]["ok"]:
        component_failures.append(LIVENESS_COMPONENT)

    anomalies: List[Dict[str, Any]] = []
    for name in component_failures:
        result = components[name]
        anomalies.append(
            _anomaly(
                "component_integrity",
                f"{name} validator failed: status={result['status']!r} "
                f"n_anomalies={result['n_anomalies']}",
                extra={"component": name, "status": result["status"]},
            )
        )

    # A broken third component must not hide a mismatch between the remaining
    # independently valid enabled components. Invalid blocks themselves are
    # excluded because their provenance has not passed its local contract.
    anomalies.extend(_compare_enabled(enabled, manifest_obj))

    anomalies.sort(
        key=lambda item: (
            str(item.get("code") or ""),
            str(item.get("message") or ""),
            _strict_canonical_json(
                {
                    key: item[key]
                    for key in sorted(item, key=lambda name: repr(name))
                    if key not in {"code", "message"}
                }
            ),
        )
    )
    total = len(anomalies)
    samples = anomalies[:max_anomaly_samples]
    if total:
        mode_state = "invalid"
    elif enabled:
        mode_state = "coherent"
    elif off:
        mode_state = "off"
    else:
        mode_state = "legacy_absent"
    ok = total == 0 and mode_state in {"legacy_absent", "off", "coherent"}
    status = mode_state if ok else "invalid"

    shared: Optional[Dict[str, Any]] = None
    if ok and enabled:
        first = _extract_shared(enabled[0], manifest_obj[enabled[0]])
        shared = {
            "compile_commands_digest": first["compile_commands_digest"],
            "n_compile_entries": first["n_compile_entries"],
            "compilers": list(first["compilers"]),
            "compiler_path": first["compiler_path"],
            "compiler_id": first["compiler_id"],
            "compiler_version": first["compiler_version"],
        }

    return {
        "ok": ok,
        "status": status,
        "mode": mode_state,
        "n_anomalies": total,
        "n_anomaly_samples": len(samples),
        "anomalies_truncated": total > len(samples),
        "anomalies": samples,
        "components": components,
        "census": {
            "enabled": list(enabled),
            "off": list(off),
            "absent": list(absent),
            "failed": list(failed),
        },
        "component_failures": component_failures,
        "shared": shared,
        "limitations": list(LIMITATIONS),
    }
