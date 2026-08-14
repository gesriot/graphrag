#!/usr/bin/env python
"""Read-only integrity audit for persisted configured Clang uses_type edges.

Validates already-published ``uses_type`` relationships and their
``clang_type_uses`` manifest block against the producer contract in
``c_clang_type_uses``. Does **not** invoke Clang, load compile_commands.json,
reindex, publish, re-run the overlay, or create compiler/AST artifacts.

Exit codes:
  0 — valid graph (including legacy_absent / off with zero configured edges)
  1 — integrity anomalies on a readable graph
  2 — unreadable/malformed graph, snapshot, or manifest

Usage:
    uv run python scripts/c_clang_type_use_graph_audit.py --graph byog_inih
    uv run python scripts/c_clang_type_use_graph_audit.py --graph byog_inih --json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


from graphrag_code.c_clang_type_uses import (  # type: ignore
    CONFIDENCE_BOUNDARY,
    EXTRACTOR,
    FACT_KIND,
    MODE,
    REL_TYPE,
    _strict_json_loads,
    validate_persisted_type_use_overlay,
)

AUDIT_MODE = "clang_type_use_graph_integrity"


class ClangTypeUseGraphAuditError(Exception):
    """Raised when the graph/snapshot/manifest cannot be read honestly."""


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes)):
        try:
            return _json_safe(value.tolist())
        except Exception:
            return str(value)
    return str(value)


def audit_rows(
    entities: Any,
    relationships: Any,
    manifest: Optional[Mapping[str, Any]] = None,
    *,
    max_anomaly_samples: int = 40,
    graph: Optional[str] = None,
    snapshot: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure dataframe/row API. Never mutates inputs or invokes Clang."""
    result = validate_persisted_type_use_overlay(
        entities,
        relationships,
        dict(manifest) if manifest is not None else None,
        max_anomaly_samples=max_anomaly_samples,
    )
    out: Dict[str, Any] = {
        "audit_mode": AUDIT_MODE,
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "mode": str(result["mode"]),
        "n_configured_edges": int(result["n_configured_edges"]),
        "n_observations_decoded": int(result["n_observations_decoded"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
        "fact_kind": FACT_KIND,
        "extractor": EXTRACTOR,
        "relationship_type": REL_TYPE,
        "overlay_mode": MODE,
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }
    if graph is not None:
        out["graph"] = graph
    if snapshot is not None:
        out["snapshot"] = snapshot
    return out


def resolve_current_snapshot(graph_root: Path) -> Tuple[Path, str, Dict[str, Any]]:
    """Resolve ``current`` → snapshot dir + parsed manifest (fail-closed)."""
    graph_root = Path(graph_root)
    if not graph_root.exists():
        raise ClangTypeUseGraphAuditError(
            f"graph root does not exist: {graph_root}"
        )
    pointer = graph_root / "current"
    if pointer.is_file():
        try:
            snap_id = pointer.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise ClangTypeUseGraphAuditError(
                f"cannot read current pointer: {pointer}: {e}"
            ) from e
        if not snap_id:
            raise ClangTypeUseGraphAuditError(
                f"empty current pointer: {pointer}"
            )
        if Path(snap_id).name != snap_id or snap_id in {".", ".."}:
            raise ClangTypeUseGraphAuditError(
                f"unsafe current snapshot id: {snap_id!r}"
            )
        snapshots_dir = (graph_root / "snapshots").resolve()
        snap_dir = (snapshots_dir / snap_id).resolve()
        if snap_dir.parent != snapshots_dir:
            raise ClangTypeUseGraphAuditError(
                f"current snapshot escapes snapshots directory: {snap_id!r}"
            )
        if not snap_dir.is_dir():
            raise ClangTypeUseGraphAuditError(
                f"current snapshot directory missing: {snap_dir}"
            )
    else:
        # Flat layout (tests / legacy): graph_root itself holds parquets.
        snap_dir = graph_root
        snap_id = ""
        if not (snap_dir / "entities.parquet").is_file():
            raise ClangTypeUseGraphAuditError(
                f"no current pointer and no entities.parquet under {graph_root}"
            )

    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ClangTypeUseGraphAuditError(
            f"snapshot missing manifest.json: {manifest_path}"
        )
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        manifest = _strict_json_loads(raw)
    except (OSError, UnicodeDecodeError) as e:
        raise ClangTypeUseGraphAuditError(
            f"cannot read manifest: {manifest_path}: {e}"
        ) from e
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        raise ClangTypeUseGraphAuditError(
            f"malformed manifest JSON: {manifest_path}: {e}"
        ) from e
    if not isinstance(manifest, dict):
        raise ClangTypeUseGraphAuditError(
            f"manifest is not a JSON object: {manifest_path}"
        )

    return snap_dir, snap_id, manifest


def _load_table(snap_dir: Path, name: str) -> List[Dict[str, Any]]:
    path = snap_dir / f"{name}.parquet"
    if not path.is_file():
        raise ClangTypeUseGraphAuditError(
            f"snapshot missing {name}.parquet: {path}"
        )
    try:
        import pandas as pd
    except ImportError as e:
        raise ClangTypeUseGraphAuditError(
            f"pandas is required to read parquet: {e}"
        ) from e
    try:
        df = pd.read_parquet(path)
    except Exception as e:
        raise ClangTypeUseGraphAuditError(
            f"cannot read {name}.parquet: {path}: {e}"
        ) from e
    try:
        return df.to_dict("records")
    except Exception as e:
        raise ClangTypeUseGraphAuditError(
            f"cannot materialize {name} rows: {e}"
        ) from e


def audit_graph_root(
    graph_root: Path,
    *,
    max_anomaly_samples: int = 40,
) -> Dict[str, Any]:
    """Graph-root API: resolve current snapshot safely and audit."""
    graph_root = Path(graph_root)
    snap_dir, snap_id, manifest = resolve_current_snapshot(graph_root)
    entities = _load_table(snap_dir, "entities")
    relationships = _load_table(snap_dir, "relationships")
    return audit_rows(
        entities,
        relationships,
        manifest,
        max_anomaly_samples=max_anomaly_samples,
        graph=str(graph_root),
        snapshot=snap_id or None,
    )


def audit_to_json(report: Mapping[str, Any]) -> str:
    """Deterministic JSON serialization of an audit report."""
    safe = _json_safe(dict(report))
    return json.dumps(
        safe,
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ) + "\n"


def format_report(report: Mapping[str, Any]) -> str:
    lines = [
        "Clang uses_type graph integrity audit (read-only; no Clang re-run)",
        f"  status={report.get('status')} ok={report.get('ok')} "
        f"mode={report.get('mode')}",
    ]
    if report.get("graph"):
        snap = report.get("snapshot") or ""
        suffix = f" @ {snap}" if snap else ""
        lines.append(f"  graph={report['graph']}{suffix}")
    lines.append(
        f"  configured_edges={report.get('n_configured_edges')} "
        f"observations_decoded={report.get('n_observations_decoded')} "
        f"anomalies={report.get('n_anomalies')}"
        + (
            f" (showing {report.get('n_anomaly_samples')})"
            if report.get("anomalies_truncated")
            else ""
        )
    )
    for anomaly in report.get("anomalies") or []:
        rid = anomaly.get("relationship_id")
        rid_s = f" rel={rid}" if rid else ""
        lines.append(
            f"  - [{anomaly.get('code')}]{rid_s}: {anomaly.get('message')}"
        )
    if report.get("ok"):
        lines.append("RESULT: PASS")
    else:
        lines.append("RESULT: FAIL")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--graph",
        type=Path,
        required=True,
        help="BYOG graph root (with current/snapshots or flat parquets)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON",
    )
    parser.add_argument(
        "--max-anomaly-samples",
        type=int,
        default=40,
        help="bound human-readable anomaly samples (exact totals always retained)",
    )
    args = parser.parse_args(argv)
    try:
        report = audit_graph_root(
            args.graph,
            max_anomaly_samples=args.max_anomaly_samples,
        )
    except ClangTypeUseGraphAuditError as e:
        print(f"c_clang_type_use_graph_audit: {e}", file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError, KeyError) as e:
        print(f"c_clang_type_use_graph_audit: {e}", file=sys.stderr)
        return 2

    if args.json:
        sys.stdout.write(audit_to_json(report))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
