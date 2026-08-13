#!/usr/bin/env python
"""Check that mutable published BYOG graphs still match their extractor.

Source port profiles intentionally build disposable graphs under
``output/port_gates``.  This companion check protects the different thing a
published ``byog_*`` root represents: local, queryable evidence.  It never
writes a published root.

The population comes from ``published_graph`` declarations in
``scripts/port_gates.json``.  Mutable roots are compared with a fresh in-memory
extraction; frozen roots are reported as exemptions and not opened.  A missing
mutable root is an explicit local-artifact skip.  A root that exists but has a
stale or malformed ``current`` snapshot is a failure.

Usage:

    uv run python scripts/published_graph_health.py --check
    uv run python scripts/published_graph_health.py --json
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "scripts" / "port_gates.json"

# These are semantic graph fields, deliberately excluding publication-only
# payload such as a file entity's full snippet or rich liveness explanation.
# They cover identity, containment/call targets, confidence, and the dynamic/C
# provenance labels that have previously caused published-artifact drift.
TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "entities": (
        "id",
        "title",
        "type",
        "source_file",
        "span",
        "confidence",
        "is_deterministic",
        "dynamic_dependent",
        "preprocessor_dependent",
    ),
    "relationships": (
        "id",
        "source",
        "target",
        "type",
        "confidence",
        "is_deterministic",
        "dynamic_dependent",
        "preprocessor_dependent",
        "resolved_target_hint",
    ),
    "text_units": ("id", "title", "source_file", "entity_id"),
}


@dataclass(frozen=True)
class PublishedGraphSpec:
    """One published graph's source and deliberate mutability policy."""

    ident: str
    source: Path
    graph: str
    indexer: str
    mode: str
    reason: str | None = None


def load_specs(manifest: Path = DEFAULT_MANIFEST) -> list[PublishedGraphSpec]:
    """Load the fail-closed published-graph declarations from the gate manifest."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from port_eval import load_gate_manifest  # type: ignore

    gates = load_gate_manifest(manifest)
    specs: list[PublishedGraphSpec] = []
    for ident, entry in gates.items():
        declared = entry["published_graph"]
        specs.append(
            PublishedGraphSpec(
                ident=ident,
                source=Path(str(entry["source"])),
                graph=str(declared["path"]),
                indexer=str(entry["indexer"]),
                mode=str(declared["mode"]),
                reason=(str(declared["reason"]) if declared.get("reason") is not None else None),
            )
        )
    return specs


def _normalize(value: Any) -> Any:
    """Normalize Python/Parquet storage variants into one JSON value."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        return _normalize(value.tolist())
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _signature(rows: Iterable[Mapping[str, Any]], fields: tuple[str, ...]) -> collections.Counter[str]:
    """Return a multiset so duplicate structural relationships remain visible."""
    return collections.Counter(
        json.dumps(
            {field: _normalize(row.get(field)) for field in fields},
            sort_keys=True,
            separators=(",", ":"),
        )
        for row in rows
    )


def _fresh_data(spec: PublishedGraphSpec, root: Path) -> dict[str, list[dict[str, Any]]]:
    source = (root / spec.source).resolve()
    if spec.indexer == "python":
        sys.path.insert(0, str(root / "scripts"))
        from mini_game_to_byog import build_byog_for_package  # type: ignore

        return build_byog_for_package(package_dir=source)
    if spec.indexer == "c":
        sys.path.insert(0, str(root / "scripts"))
        from c_preprocessor import annotate_byog  # type: ignore
        from extract_c import build_c_byog  # type: ignore

        data = build_c_byog(source)
        # Published C graphs use the portable no-compiler liveness policy.
        annotate_byog(data, source, use_compiler_builtins=False, graph_dir=None)
        return data
    raise ValueError(f"{spec.ident}: unknown indexer {spec.indexer!r}")


def _strict_manifest_object(path: Path) -> dict[str, Any]:
    def reject_constant(token: str) -> None:
        raise ValueError(f"non-finite JSON constant {token}")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate JSON object key {key!r}")
            out[key] = value
        return out

    raw = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )
    if not isinstance(raw, dict):
        raise ValueError(f"manifest is not a JSON object: {path}")
    return raw


def _published_data(
    graph_root: Path,
) -> tuple[str, dict[str, list[dict[str, Any]]], dict[str, Any]]:
    pointer = graph_root / "current"
    if not pointer.is_file():
        raise FileNotFoundError(f"missing current pointer: {pointer}")
    snapshot = pointer.read_text(encoding="utf-8").strip()
    if not snapshot:
        raise ValueError(f"empty current pointer: {pointer}")
    if Path(snapshot).name != snapshot or snapshot in {".", ".."}:
        raise ValueError(f"unsafe current snapshot id: {snapshot!r}")
    snapshots_dir = (graph_root / "snapshots").resolve()
    base = (snapshots_dir / snapshot).resolve()
    if base.parent != snapshots_dir:
        raise ValueError(f"current snapshot escapes snapshots directory: {snapshot!r}")
    if not base.is_dir():
        raise FileNotFoundError(f"current snapshot not found: {base}")
    manifest_path = base / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"current snapshot missing manifest: {manifest_path}")
    manifest = _strict_manifest_object(manifest_path)
    data: dict[str, list[dict[str, Any]]] = {}
    for table in TABLE_FIELDS:
        path = base / f"{table}.parquet"
        if not path.is_file():
            raise FileNotFoundError(f"current snapshot missing {table}: {path}")
        data[table] = pd.read_parquet(path).to_dict("records")
    obs_path = base / "call_observations.parquet"
    if obs_path.is_file():
        data["call_observations"] = pd.read_parquet(obs_path).to_dict("records")
    return snapshot, data, manifest


def _type_use_integrity(
    published: dict[str, list[dict[str, Any]]],
    manifest: Mapping[str, Any],
    *,
    indexer: str,
) -> dict[str, Any] | None:
    """Read-only configured uses_type integrity for C published graphs.

    Never invokes Clang and never re-runs the overlay. Legacy / default-off
    C graphs with zero configured edges pass as ``legacy_absent`` / ``off``.
    Non-C indexers skip this check (returns None).
    """
    if indexer != "c":
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    from c_clang_type_uses import validate_persisted_type_use_overlay  # type: ignore

    result = validate_persisted_type_use_overlay(
        published.get("entities") or [],
        published.get("relationships") or [],
        manifest,
    )
    return {
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "mode": str(result["mode"]),
        "n_configured_edges": int(result["n_configured_edges"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
    }


def _type_shape_integrity(
    published: dict[str, list[dict[str, Any]]],
    manifest: Mapping[str, Any],
    *,
    indexer: str,
) -> dict[str, Any] | None:
    """Read-only configured type-shape integrity for C published graphs.

    Never invokes Clang, never re-runs the overlay, never reindexes. Legacy /
    default-off C graphs with zero ``clang_shape_*`` fields pass as
    ``legacy_absent`` / ``off``. Non-C indexers skip this check (returns None).
    """
    if indexer != "c":
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    from c_clang_type_shapes import (  # type: ignore
        validate_persisted_type_shape_overlay,
    )

    result = validate_persisted_type_shape_overlay(
        published.get("entities") or [], manifest
    )
    return {
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "mode": str(result["mode"]),
        "n_decorated_entities": int(result["n_decorated_entities"]),
        "n_members_validated": int(result["n_members_validated"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
    }


def _type_integrity(
    published: dict[str, list[dict[str, Any]]],
    manifest: Mapping[str, Any],
    *,
    indexer: str,
) -> dict[str, Any] | None:
    """Read-only configured type-declaration integrity for C published graphs.

    Never invokes Clang, never re-runs the overlay, never reindexes. Legacy /
    default-off C graphs with zero ``clang_type_*`` fields pass as
    ``legacy_absent`` / ``off``. Non-C indexers skip this check (returns None).
    """
    if indexer != "c":
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    from c_clang_types import validate_persisted_type_overlay  # type: ignore

    result = validate_persisted_type_overlay(
        published.get("entities") or [], manifest
    )
    return {
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "mode": str(result["mode"]),
        "n_decorated_entities": int(result["n_decorated_entities"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
    }


def _signature_integrity(
    published: dict[str, list[dict[str, Any]]],
    manifest: Mapping[str, Any],
    *,
    indexer: str,
) -> dict[str, Any] | None:
    """Read-only configured function-signature integrity for C published graphs.

    Never invokes Clang, never re-runs the overlay, never reindexes. Legacy /
    default-off C graphs with zero signature fields pass as ``legacy_absent``
    / ``off``. Non-C indexers skip this check (returns None).
    """
    if indexer != "c":
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    from c_clang_signatures import validate_persisted_signature_overlay  # type: ignore

    result = validate_persisted_signature_overlay(
        published.get("entities") or [],
        manifest,
    )
    return {
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "mode": str(result["mode"]),
        "n_decorated_entities": int(result["n_decorated_entities"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
    }


def _call_integrity(
    published: dict[str, list[dict[str, Any]]],
    manifest: Mapping[str, Any],
    *,
    indexer: str,
) -> dict[str, Any] | None:
    """Read-only configured-call integrity for C published graphs.

    Never invokes Clang, never re-runs the overlay, never reindexes. Legacy /
    default-off C graphs with zero ``clang_call_*`` fields pass as
    ``legacy_absent`` / ``off``. Non-C indexers skip this check (returns None).
    """
    if indexer != "c":
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    from c_clang_calls import validate_persisted_call_overlay  # type: ignore

    result = validate_persisted_call_overlay(
        published.get("relationships") or [], manifest
    )
    return {
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "mode": str(result["mode"]),
        "n_decorated_relationships": int(result["n_decorated_relationships"]),
        "n_calls": int(result["n_calls"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
    }


def _compiler_dependency_integrity(
    published: dict[str, list[dict[str, Any]]],
    manifest: Mapping[str, Any],
    *,
    indexer: str,
) -> dict[str, Any] | None:
    """Read-only compiler-dependency integrity for C published graphs.

    Never invokes a compiler, never re-runs the overlay, never reindexes.
    Legacy / default-off C graphs with zero dependency edges pass as
    ``legacy_absent`` / ``off``. Non-C indexers skip this check (returns None).
    """
    if indexer != "c":
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    from c_compiler_facts import (  # type: ignore
        validate_persisted_compiler_dependency_overlay,
    )

    result = validate_persisted_compiler_dependency_overlay(
        published.get("entities") or [],
        published.get("relationships") or [],
        manifest,
    )
    return {
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "mode": str(result["mode"]),
        "n_decorated_relationships": int(result["n_decorated_relationships"]),
        "n_translation_units": int(result["n_translation_units"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
    }


def _compiler_include_integrity(
    published: dict[str, list[dict[str, Any]]],
    manifest: Mapping[str, Any],
    *,
    indexer: str,
) -> dict[str, Any] | None:
    """Read-only compiler-include integrity for C published graphs.

    Never invokes a compiler, never re-runs the overlay, never reindexes.
    Legacy / default-off C graphs with zero include edges pass as
    ``legacy_absent`` / ``off``. Non-C indexers skip this check (returns None).
    """
    if indexer != "c":
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    from c_compiler_includes import (  # type: ignore
        validate_persisted_compiler_include_overlay,
    )

    result = validate_persisted_compiler_include_overlay(
        published.get("entities") or [],
        published.get("relationships") or [],
        manifest,
    )
    return {
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "mode": str(result["mode"]),
        "n_decorated_relationships": int(result["n_decorated_relationships"]),
        "n_translation_units": int(result["n_translation_units"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
    }


def _preprocessor_liveness_integrity(
    published: dict[str, list[dict[str, Any]]],
    manifest: Mapping[str, Any],
    *,
    indexer: str,
) -> dict[str, Any] | None:
    """Read-only persisted preprocessor-liveness integrity for C graphs.

    Never reanalyses sources, never invokes a compiler, never restamps.
    Legacy C graphs with no material stamps pass as ``legacy_absent``.
    Non-C indexers skip this check (returns None).
    """
    if indexer != "c":
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    from c_preprocessor import (  # type: ignore
        validate_persisted_preprocessor_liveness,
    )

    result = validate_persisted_preprocessor_liveness(
        published.get("entities") or [],
        published.get("relationships") or [],
        published.get("call_observations"),
        manifest,
    )
    return {
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "mode": str(result["mode"]),
        "eval_mode": str(result["eval_mode"]),
        "n_stamped_rows": int(result["n_stamped_rows"]),
        "n_call_observations": int(result["n_call_observations"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
    }


def _overlay_coherence_integrity(
    published: dict[str, list[dict[str, Any]]],
    manifest: Mapping[str, Any],
    *,
    indexer: str,
) -> dict[str, Any] | None:
    """Read-only cross-overlay configuration coherence for C graphs.

    Never invokes a compiler, never reindexes, never restamps. Non-C
    indexers skip this check (returns None).
    """
    if indexer != "c":
        return None
    sys.path.insert(0, str(ROOT / "scripts"))
    from c_overlay_coherence import (  # type: ignore
        validate_persisted_c_overlay_coherence,
    )

    result = validate_persisted_c_overlay_coherence(
        published.get("entities") or [],
        published.get("relationships") or [],
        published.get("call_observations"),
        manifest,
    )
    return {
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "mode": str(result["mode"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
        "census": dict(result["census"]),
        "shared": result.get("shared"),
    }


def check_spec(
    spec: PublishedGraphSpec,
    *,
    root: Path = ROOT,
    graph_root: Path | None = None,
) -> dict[str, Any]:
    """Check one spec without modifying any published artifact."""
    if spec.mode == "frozen":
        return {
            "id": spec.ident,
            "graph": spec.graph,
            "status": "exempt",
            "reason": spec.reason,
        }
    if spec.mode != "mutable":
        raise ValueError(f"{spec.ident}: unsupported published graph mode {spec.mode!r}")

    published_root = graph_root if graph_root is not None else root / spec.graph
    if not published_root.exists():
        return {
            "id": spec.ident,
            "graph": spec.graph,
            "status": "skipped",
            "reason": "published mutable graph is absent locally",
        }

    try:
        snapshot, published, published_manifest = _published_data(published_root)
        fresh = _fresh_data(spec, root)
    except (OSError, ValueError, KeyError) as error:
        return {
            "id": spec.ident,
            "graph": spec.graph,
            "status": "fail",
            "reason": str(error),
        }

    mismatches: dict[str, dict[str, int]] = {}
    for table, fields in TABLE_FIELDS.items():
        expected = _signature(fresh[table], fields)
        actual = _signature(published[table], fields)
        missing = int(sum((expected - actual).values()))
        extra = int(sum((actual - expected).values()))
        if missing or extra:
            mismatches[table] = {
                "fresh_rows": int(sum(expected.values())),
                "published_rows": int(sum(actual.values())),
                "missing_from_published": missing,
                "extra_in_published": extra,
            }

    type_use = _type_use_integrity(
        published, published_manifest, indexer=spec.indexer
    )
    type_shape = _type_shape_integrity(
        published, published_manifest, indexer=spec.indexer
    )
    type_decl = _type_integrity(
        published, published_manifest, indexer=spec.indexer
    )
    signature = _signature_integrity(
        published, published_manifest, indexer=spec.indexer
    )
    call = _call_integrity(
        published, published_manifest, indexer=spec.indexer
    )
    compiler_deps = _compiler_dependency_integrity(
        published, published_manifest, indexer=spec.indexer
    )
    compiler_includes = _compiler_include_integrity(
        published, published_manifest, indexer=spec.indexer
    )
    liveness = _preprocessor_liveness_integrity(
        published, published_manifest, indexer=spec.indexer
    )
    coherence = _overlay_coherence_integrity(
        published, published_manifest, indexer=spec.indexer
    )

    def _with_overlays(result: dict[str, Any]) -> dict[str, Any]:
        if type_use is not None:
            result["clang_type_use_integrity"] = type_use
        if type_shape is not None:
            result["clang_type_shape_integrity"] = type_shape
        if type_decl is not None:
            result["clang_type_integrity"] = type_decl
        if signature is not None:
            result["clang_signature_integrity"] = signature
        if call is not None:
            result["clang_call_integrity"] = call
        if compiler_deps is not None:
            result["compiler_dependency_integrity"] = compiler_deps
        if compiler_includes is not None:
            result["compiler_include_integrity"] = compiler_includes
        if liveness is not None:
            result["preprocessor_liveness_integrity"] = liveness
        if coherence is not None:
            result["c_overlay_coherence_integrity"] = coherence
        return result

    if mismatches:
        return _with_overlays(
            {
                "id": spec.ident,
                "graph": spec.graph,
                "snapshot": snapshot,
                "status": "fail",
                "reason": "published graph disagrees with current extractor",
                "mismatches": mismatches,
            }
        )

    if type_use is not None and not type_use["ok"]:
        return _with_overlays(
            {
                "id": spec.ident,
                "graph": spec.graph,
                "snapshot": snapshot,
                "status": "fail",
                "reason": "configured uses_type integrity anomalies",
            }
        )

    if type_shape is not None and not type_shape["ok"]:
        return _with_overlays(
            {
                "id": spec.ident,
                "graph": spec.graph,
                "snapshot": snapshot,
                "status": "fail",
                "reason": "configured type-shape integrity anomalies",
            }
        )

    if type_decl is not None and not type_decl["ok"]:
        return _with_overlays(
            {
                "id": spec.ident,
                "graph": spec.graph,
                "snapshot": snapshot,
                "status": "fail",
                "reason": "configured type-declaration integrity anomalies",
            }
        )

    if signature is not None and not signature["ok"]:
        return _with_overlays(
            {
                "id": spec.ident,
                "graph": spec.graph,
                "snapshot": snapshot,
                "status": "fail",
                "reason": "configured function-signature integrity anomalies",
            }
        )

    if call is not None and not call["ok"]:
        return _with_overlays(
            {
                "id": spec.ident,
                "graph": spec.graph,
                "snapshot": snapshot,
                "status": "fail",
                "reason": "configured call integrity anomalies",
            }
        )

    if compiler_deps is not None and not compiler_deps["ok"]:
        return _with_overlays(
            {
                "id": spec.ident,
                "graph": spec.graph,
                "snapshot": snapshot,
                "status": "fail",
                "reason": "compiler-dependency integrity anomalies",
            }
        )

    if compiler_includes is not None and not compiler_includes["ok"]:
        return _with_overlays(
            {
                "id": spec.ident,
                "graph": spec.graph,
                "snapshot": snapshot,
                "status": "fail",
                "reason": "compiler-include integrity anomalies",
            }
        )

    if liveness is not None and not liveness["ok"]:
        return _with_overlays(
            {
                "id": spec.ident,
                "graph": spec.graph,
                "snapshot": snapshot,
                "status": "fail",
                "reason": "preprocessor-liveness integrity anomalies",
            }
        )

    if coherence is not None and not coherence["ok"]:
        return _with_overlays(
            {
                "id": spec.ident,
                "graph": spec.graph,
                "snapshot": snapshot,
                "status": "fail",
                "reason": "compiler-overlay coherence anomalies",
            }
        )

    return _with_overlays(
        {
            "id": spec.ident,
            "graph": spec.graph,
            "snapshot": snapshot,
            "status": "pass",
        }
    )


def build_report(manifest: Path = DEFAULT_MANIFEST, root: Path = ROOT) -> dict[str, Any]:
    """Check every declared root; only absent mutable artifacts may skip."""
    specs = load_specs(manifest)
    results = [check_spec(spec, root=root) for spec in specs]
    failed = [result for result in results if result["status"] == "fail"]
    skipped = [result for result in results if result["status"] == "skipped"]
    return {
        "manifest": str(manifest),
        "results": results,
        "mutable": sum(spec.mode == "mutable" for spec in specs),
        "frozen": sum(spec.mode == "frozen" for spec in specs),
        "failed": len(failed),
        "skipped": len(skipped),
        "ok": not failed,
    }


def format_report(report: Mapping[str, Any]) -> str:
    lines = ["Published graph health (mutable local artifacts compared with current extractor)"]
    for result in report["results"]:
        suffix = f" — {result.get('reason')}" if result.get("reason") else ""
        snapshot = f" @ {result['snapshot']}" if result.get("snapshot") else ""
        lines.append(f"  [{result['status'].upper()}] {result['id']}: {result['graph']}{snapshot}{suffix}")
        for table, mismatch in result.get("mismatches", {}).items():
            lines.append(
                f"    {table}: fresh={mismatch['fresh_rows']} published={mismatch['published_rows']} "
                f"missing={mismatch['missing_from_published']} extra={mismatch['extra_in_published']}"
            )
        type_use = result.get("clang_type_use_integrity")
        if isinstance(type_use, Mapping):
            lines.append(
                f"    clang_type_use_integrity: status={type_use.get('status')} "
                f"ok={type_use.get('ok')} edges={type_use.get('n_configured_edges')} "
                f"anomalies={type_use.get('n_anomalies')}"
            )
        type_shape = result.get("clang_type_shape_integrity")
        if isinstance(type_shape, Mapping):
            lines.append(
                f"    clang_type_shape_integrity: status={type_shape.get('status')} "
                f"ok={type_shape.get('ok')} "
                f"decorated={type_shape.get('n_decorated_entities')} "
                f"anomalies={type_shape.get('n_anomalies')}"
            )
        type_decl = result.get("clang_type_integrity")
        if isinstance(type_decl, Mapping):
            lines.append(
                f"    clang_type_integrity: status={type_decl.get('status')} "
                f"ok={type_decl.get('ok')} "
                f"decorated={type_decl.get('n_decorated_entities')} "
                f"anomalies={type_decl.get('n_anomalies')}"
            )
        signature = result.get("clang_signature_integrity")
        if isinstance(signature, Mapping):
            lines.append(
                f"    clang_signature_integrity: status={signature.get('status')} "
                f"ok={signature.get('ok')} "
                f"decorated={signature.get('n_decorated_entities')} "
                f"anomalies={signature.get('n_anomalies')}"
            )
        call = result.get("clang_call_integrity")
        if isinstance(call, Mapping):
            lines.append(
                f"    clang_call_integrity: status={call.get('status')} "
                f"ok={call.get('ok')} "
                f"decorated={call.get('n_decorated_relationships')} "
                f"anomalies={call.get('n_anomalies')}"
            )
        compiler_deps = result.get("compiler_dependency_integrity")
        if isinstance(compiler_deps, Mapping):
            lines.append(
                f"    compiler_dependency_integrity: "
                f"status={compiler_deps.get('status')} "
                f"ok={compiler_deps.get('ok')} "
                f"decorated={compiler_deps.get('n_decorated_relationships')} "
                f"anomalies={compiler_deps.get('n_anomalies')}"
            )
        compiler_includes = result.get("compiler_include_integrity")
        if isinstance(compiler_includes, Mapping):
            lines.append(
                f"    compiler_include_integrity: "
                f"status={compiler_includes.get('status')} "
                f"ok={compiler_includes.get('ok')} "
                f"decorated={compiler_includes.get('n_decorated_relationships')} "
                f"anomalies={compiler_includes.get('n_anomalies')}"
            )
        liveness = result.get("preprocessor_liveness_integrity")
        if isinstance(liveness, Mapping):
            lines.append(
                f"    preprocessor_liveness_integrity: "
                f"status={liveness.get('status')} "
                f"ok={liveness.get('ok')} "
                f"stamped={liveness.get('n_stamped_rows')} "
                f"anomalies={liveness.get('n_anomalies')}"
            )
        coherence = result.get("c_overlay_coherence_integrity")
        if isinstance(coherence, Mapping):
            lines.append(
                f"    c_overlay_coherence_integrity: "
                f"status={coherence.get('status')} "
                f"ok={coherence.get('ok')} "
                f"anomalies={coherence.get('n_anomalies')}"
            )
    lines.append(
        f"  declared mutable={report['mutable']} frozen-exempt={report['frozen']} "
        f"local-skips={report['skipped']}"
    )
    if not report["ok"]:
        lines.append("RESULT: FAIL")
    elif report["skipped"]:
        lines.append("PASS WITH LOCAL ARTIFACT SKIPS")
    else:
        lines.append("PASS")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check all declared published graphs")
    parser.add_argument("--json", action="store_true", help="emit the full machine-readable report")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    if not args.check and not args.json:
        parser.error("choose --check or --json")
    try:
        report = build_report(args.manifest)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"published graph health: FAIL\n{error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
