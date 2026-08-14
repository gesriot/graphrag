#!/usr/bin/env python
"""Read-only integrity audit for persisted configured Clang type-shape fields.

Validates already-published ``clang_shape_*`` entity evidence and its
``clang_type_shapes`` manifest block against the producer contract in
``c_clang_type_shapes``. Does **not** invoke Clang, load compile_commands.json,
build an AST capture, reindex, publish, re-run the overlay, rewrite byog_*
roots, or create compiler/AST artifacts. Nothing is ever repaired.

Supported states:
  legacy_absent – no manifest block and no ``clang_shape_*`` fields
  off           – ``mode=off`` / ``enabled=false`` with no shape fields
  enabled       – ``mode=configured_clang_type_shapes`` fully validated

Exit codes:
  0 – audit completed and passed
  1 – integrity violations found
  2 – graph/snapshot/load/schema error

Usage:
    uv run python scripts/c_clang_type_shape_graph_audit.py --graph byog_cjson
    uv run python scripts/c_clang_type_shape_graph_audit.py --graph byog_cjson \\
        --snapshot 20260813-081031-10c2c75b --output shape_audit.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


from graphrag_code.c_clang_type_shapes import (  # type: ignore
    CONFIDENCE_BOUNDARY,
    EXTRACTOR,
    FACT_KIND,
    HARD_EQUALITY,
    LIMITATIONS,
    MODE,
    strict_json_loads,
    validate_persisted_type_shape_overlay,
)

AUDIT_MODE = "clang_type_shape_graph_integrity"

# Inputs whose bytes must be identical before and after the audit.
_READ_ONLY_FILES = (
    "manifest.json",
    "entities.parquet",
    "relationships.parquet",
    "text_units.parquet",
)


class ClangTypeShapeGraphAuditError(Exception):
    """Raised when the graph/snapshot/manifest cannot be read honestly."""


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (str, int, bool, float)):
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
    manifest: Optional[Mapping[str, Any]] = None,
    *,
    max_anomaly_samples: int = 40,
    graph: Optional[str] = None,
    snapshot: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure dataframe/row API. Never mutates inputs or invokes Clang."""
    result = validate_persisted_type_shape_overlay(
        entities,
        dict(manifest) if manifest is not None else None,
        max_anomaly_samples=max_anomaly_samples,
    )
    out: Dict[str, Any] = {
        "audit_mode": AUDIT_MODE,
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "state": str(result["status"]),
        "mode": str(result["mode"]),
        "classification": str(result["mode"]),
        "n_entities": int(result["n_entities"]),
        "n_decorated_entities": int(result["n_decorated_entities"]),
        "n_shape_field_carriers": int(result["n_shape_field_carriers"]),
        "n_members_validated": int(result["n_members_validated"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
        "n_violations": int(result["n_anomalies"]),
        "violations": list(result["anomalies"]),
        "counts": dict(result["counts"]),
        "provenance": dict(result["provenance"]),
        "overlay_mode": MODE,
        "fact_kind": FACT_KIND,
        "extractor": EXTRACTOR,
        "hard_equality": HARD_EQUALITY,
        "limitations": list(LIMITATIONS),
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }
    if graph is not None:
        out["graph"] = graph
    if snapshot is not None:
        out["snapshot"] = snapshot
    return out


def resolve_snapshot(
    graph_root: Path, snapshot: Optional[str] = None
) -> Tuple[Path, str, Dict[str, Any]]:
    """Resolve ``current`` (or an explicit snapshot) to dir + manifest."""
    graph_root = Path(graph_root)
    if not graph_root.exists():
        raise ClangTypeShapeGraphAuditError(
            f"graph root does not exist: {graph_root}"
        )
    snapshots_dir = (graph_root / "snapshots").resolve()
    if snapshot is not None:
        snap_id = snapshot.strip()
        if not snap_id:
            raise ClangTypeShapeGraphAuditError("empty --snapshot id")
        if Path(snap_id).name != snap_id or snap_id in {".", ".."}:
            raise ClangTypeShapeGraphAuditError(
                f"unsafe snapshot id: {snap_id!r}"
            )
        snap_dir = (snapshots_dir / snap_id).resolve()
        if snap_dir.parent != snapshots_dir:
            raise ClangTypeShapeGraphAuditError(
                f"snapshot escapes snapshots directory: {snap_id!r}"
            )
        if not snap_dir.is_dir():
            raise ClangTypeShapeGraphAuditError(
                f"snapshot directory missing: {snap_dir}"
            )
    else:
        pointer = graph_root / "current"
        if pointer.is_file():
            try:
                snap_id = pointer.read_text(encoding="utf-8").strip()
            except OSError as e:
                raise ClangTypeShapeGraphAuditError(
                    f"cannot read current pointer: {pointer}: {e}"
                ) from e
            if not snap_id:
                raise ClangTypeShapeGraphAuditError(
                    f"empty current pointer: {pointer}"
                )
            if Path(snap_id).name != snap_id or snap_id in {".", ".."}:
                raise ClangTypeShapeGraphAuditError(
                    f"unsafe current snapshot id: {snap_id!r}"
                )
            snap_dir = (snapshots_dir / snap_id).resolve()
            if snap_dir.parent != snapshots_dir:
                raise ClangTypeShapeGraphAuditError(
                    f"current snapshot escapes snapshots directory: {snap_id!r}"
                )
            if not snap_dir.is_dir():
                raise ClangTypeShapeGraphAuditError(
                    f"current snapshot directory missing: {snap_dir}"
                )
        else:
            # Flat layout (tests / legacy): graph_root itself holds parquets.
            snap_dir = graph_root
            snap_id = ""
            if not (snap_dir / "entities.parquet").is_file():
                raise ClangTypeShapeGraphAuditError(
                    f"no current pointer and no entities.parquet under "
                    f"{graph_root}"
                )

    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ClangTypeShapeGraphAuditError(
            f"snapshot missing manifest.json: {manifest_path}"
        )
    try:
        manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise ClangTypeShapeGraphAuditError(
            f"cannot read manifest: {manifest_path}: {e}"
        ) from e
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        raise ClangTypeShapeGraphAuditError(
            f"malformed manifest JSON: {manifest_path}: {e}"
        ) from e
    if not isinstance(manifest, dict):
        raise ClangTypeShapeGraphAuditError(
            f"manifest is not a JSON object: {manifest_path}"
        )
    return snap_dir, snap_id, manifest


def _load_entities(snap_dir: Path) -> List[Dict[str, Any]]:
    path = snap_dir / "entities.parquet"
    if not path.is_file():
        raise ClangTypeShapeGraphAuditError(
            f"snapshot missing entities.parquet: {path}"
        )
    try:
        import pandas as pd
    except ImportError as e:
        raise ClangTypeShapeGraphAuditError(
            f"pandas is required to read parquet: {e}"
        ) from e
    try:
        df = pd.read_parquet(path)
    except Exception as e:
        raise ClangTypeShapeGraphAuditError(
            f"cannot read entities.parquet: {path}: {e}"
        ) from e
    try:
        return df.to_dict("records")
    except Exception as e:
        raise ClangTypeShapeGraphAuditError(
            f"cannot materialize entity rows: {e}"
        ) from e


def read_only_fingerprint(graph_root: Path, snap_dir: Path) -> Dict[str, str]:
    """SHA-256 of every graph input the audit reads, plus the directory shape."""
    fingerprint: Dict[str, str] = {}
    for name in _READ_ONLY_FILES:
        path = snap_dir / name
        if not path.is_file():
            fingerprint[name] = "absent"
            continue
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    digest.update(chunk)
        except OSError as e:
            raise ClangTypeShapeGraphAuditError(
                f"cannot fingerprint {path}: {e}"
            ) from e
        fingerprint[name] = digest.hexdigest()
    pointer = Path(graph_root) / "current"
    if pointer.is_file():
        try:
            fingerprint["current"] = hashlib.sha256(
                pointer.read_bytes()
            ).hexdigest()
        except OSError as e:
            raise ClangTypeShapeGraphAuditError(
                f"cannot fingerprint {pointer}: {e}"
            ) from e
    else:
        fingerprint["current"] = "absent"
    snapshots_dir = Path(graph_root) / "snapshots"
    if snapshots_dir.is_dir():
        try:
            listing = sorted(p.name for p in snapshots_dir.iterdir())
        except OSError as e:
            raise ClangTypeShapeGraphAuditError(
                f"cannot list {snapshots_dir}: {e}"
            ) from e
        fingerprint["snapshots_dir"] = hashlib.sha256(
            "\n".join(listing).encode("utf-8")
        ).hexdigest()
    else:
        fingerprint["snapshots_dir"] = "absent"
    return fingerprint


def audit_graph_root(
    graph_root: Path,
    *,
    snapshot: Optional[str] = None,
    max_anomaly_samples: int = 40,
) -> Dict[str, Any]:
    """Graph-root API: resolve the snapshot, audit, prove nothing changed."""
    graph_root = Path(graph_root)
    snap_dir, snap_id, manifest = resolve_snapshot(graph_root, snapshot)
    before = read_only_fingerprint(graph_root, snap_dir)
    entities = _load_entities(snap_dir)
    report = audit_rows(
        entities,
        manifest,
        max_anomaly_samples=max_anomaly_samples,
        graph=str(graph_root),
        snapshot=snap_id or None,
    )
    after = read_only_fingerprint(graph_root, snap_dir)
    changed = sorted(
        name for name in before if before[name] != after.get(name)
    )
    report["read_only_verification"] = {
        "verified": not changed,
        "method": "sha256 of graph inputs before and after the audit",
        "inputs": sorted(before),
        "changed_inputs": changed,
        "fingerprint": dict(sorted(after.items())),
    }
    if changed:
        report["ok"] = False
        report["status"] = "invalid"
        report["state"] = "invalid"
        report["anomalies"] = list(report["anomalies"]) + [
            {
                "code": "read_only_violation",
                "message": f"graph inputs changed during the audit: {changed}",
            }
        ]
        report["n_anomalies"] = int(report["n_anomalies"]) + 1
        report["n_anomaly_samples"] = len(report["anomalies"])
        report["n_violations"] = report["n_anomalies"]
        report["violations"] = list(report["anomalies"])
    return report


def audit_to_json(report: Mapping[str, Any]) -> str:
    """Deterministic JSON serialization of an audit report."""
    return json.dumps(
        _json_safe(dict(report)),
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ) + "\n"


def _validate_output_path(output: Path, graph_root: Path) -> None:
    """Refuse report output anywhere inside the graph being audited."""
    try:
        resolved_output = Path(output).resolve()
        resolved_graph = Path(graph_root).resolve()
        resolved_output.relative_to(resolved_graph)
    except ValueError:
        return
    except OSError as e:
        raise ClangTypeShapeGraphAuditError(
            f"cannot resolve output path {output}: {e}"
        ) from e
    raise ClangTypeShapeGraphAuditError(
        f"--output must be outside the audited graph root: {output}"
    )


def format_report(report: Mapping[str, Any]) -> str:
    lines = [
        "Clang type-shape graph integrity audit (read-only; no Clang re-run)",
        f"  status={report.get('status')} ok={report.get('ok')} "
        f"mode={report.get('mode')}",
    ]
    if report.get("graph"):
        snap = report.get("snapshot") or ""
        suffix = f" @ {snap}" if snap else ""
        lines.append(f"  graph={report['graph']}{suffix}")
    lines.append(
        f"  decorated_entities={report.get('n_decorated_entities')} "
        f"members={report.get('n_members_validated')} "
        f"anomalies={report.get('n_anomalies')}"
        + (
            f" (showing {report.get('n_anomaly_samples')})"
            if report.get("anomalies_truncated")
            else ""
        )
    )
    provenance = report.get("provenance") or {}
    if provenance.get("compile_commands_digest"):
        lines.append(
            f"  digest={provenance.get('compile_commands_digest')} "
            f"compile_entries={provenance.get('n_compile_entries')}"
        )
    read_only = report.get("read_only_verification")
    if isinstance(read_only, Mapping):
        lines.append(
            f"  read_only_verified={read_only.get('verified')} "
            f"(changed={read_only.get('changed_inputs')})"
        )
    for anomaly in report.get("anomalies") or []:
        eid = anomaly.get("entity_id")
        eid_s = f" entity={eid}" if eid else ""
        lines.append(
            f"  - [{anomaly.get('code')}]{eid_s}: {anomaly.get('message')}"
        )
    lines.append("RESULT: PASS" if report.get("ok") else "RESULT: FAIL")
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
        "--snapshot",
        type=str,
        default=None,
        help="audit this snapshot id instead of the current pointer",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="write the deterministic JSON report to this path",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    parser.add_argument(
        "--max-anomaly-samples",
        type=int,
        default=40,
        help="bound anomaly samples (exact totals always retained)",
    )
    args = parser.parse_args(argv)
    try:
        if args.output is not None:
            _validate_output_path(args.output, args.graph)
        report = audit_graph_root(
            args.graph,
            snapshot=args.snapshot,
            max_anomaly_samples=args.max_anomaly_samples,
        )
    except ClangTypeShapeGraphAuditError as e:
        print(f"c_clang_type_shape_graph_audit: {e}", file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError, KeyError) as e:
        print(f"c_clang_type_shape_graph_audit: {e}", file=sys.stderr)
        return 2

    text = audit_to_json(report)
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        except OSError as e:
            print(
                f"c_clang_type_shape_graph_audit: cannot write output: {e}",
                file=sys.stderr,
            )
            return 2
    if args.json:
        sys.stdout.write(text)
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
