#!/usr/bin/env python
"""Read-only snapshot-wide coherence audit for persisted C compiler overlays.

Validates that every enabled compiler-backed overlay in one snapshot shares
the same compile-database digest, compile-entry census, and normalized
compiler identity. Component row contracts stay in the existing overlay
validators; this CLI reports those statuses separately from cross-overlay
coherence anomalies.

Does **not** invoke a compiler, read compile_commands.json or C/header
sources, reconstruct overlay facts, compare provenance with the current
host, reindex, repair, publish, or rewrite byog_* roots.

Supported states:
  legacy_absent – all seven compiler-backed blocks absent, no carrier evidence
  off           – every present compiler-backed overlay is off
  coherent      – one or more overlays enabled and shared fields agree
  invalid       – component integrity failure or cross-overlay mismatch

Exit codes:
  0 – audit completed and passed
  1 – integrity/coherence violations found
  2 – graph/snapshot/load/schema/output-path error

Usage:
    uv run python scripts/c_overlay_coherence_graph_audit.py --graph byog_cjson
    uv run python scripts/c_overlay_coherence_graph_audit.py --graph byog_cjson \\
        --snapshot 20260808-153310-5b68f044 --output coherence_audit.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


from graphrag_code.c_overlay_coherence import (  # type: ignore
    LIMITATIONS,
    validate_persisted_c_overlay_coherence,
)
from graphrag_code.c_preprocessor import strict_json_loads  # type: ignore

AUDIT_MODE = "c_overlay_coherence_graph_integrity"

_READ_ONLY_FILES = (
    "manifest.json",
    "entities.parquet",
    "relationships.parquet",
    "text_units.parquet",
    "call_observations.parquet",
)


class OverlayCoherenceGraphAuditError(Exception):
    """Raised when the graph/snapshot/manifest cannot be read honestly."""


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (str, int, bool, float)):
        return value
    if isinstance(value, Mapping):
        return {
            str(k): _json_safe(v)
            for k, v in sorted(value.items(), key=lambda item: repr(item[0]))
        }
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
    call_observations: Any = None,
    manifest: Optional[Mapping[str, Any]] = None,
    *,
    max_anomaly_samples: int = 40,
    graph: Optional[str] = None,
    snapshot: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure dataframe/row API. Never mutates inputs or invokes a compiler."""
    result = validate_persisted_c_overlay_coherence(
        entities,
        relationships,
        call_observations,
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
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
        "n_violations": int(result["n_anomalies"]),
        "violations": list(result["anomalies"]),
        "components": dict(result["components"]),
        "census": dict(result["census"]),
        "component_failures": list(result["component_failures"]),
        "shared": result.get("shared"),
        "limitations": list(LIMITATIONS),
    }
    if graph is not None:
        out["graph"] = graph
    if snapshot is not None:
        out["snapshot"] = snapshot
    return out


def resolve_snapshot(
    graph_root: Path, snapshot: Optional[str] = None
) -> Tuple[Path, str, Dict[str, Any]]:
    graph_root = Path(graph_root)
    if not graph_root.exists():
        raise OverlayCoherenceGraphAuditError(
            f"graph root does not exist: {graph_root}"
        )
    snapshots_dir = (graph_root / "snapshots").resolve()
    if snapshot is not None:
        snap_id = snapshot.strip()
        if not snap_id:
            raise OverlayCoherenceGraphAuditError("empty --snapshot id")
        if Path(snap_id).name != snap_id or snap_id in {".", ".."}:
            raise OverlayCoherenceGraphAuditError(
                f"unsafe snapshot id: {snap_id!r}"
            )
        snap_dir = (snapshots_dir / snap_id).resolve()
        if snap_dir.parent != snapshots_dir:
            raise OverlayCoherenceGraphAuditError(
                f"snapshot escapes snapshots directory: {snap_id!r}"
            )
        if not snap_dir.is_dir():
            raise OverlayCoherenceGraphAuditError(
                f"snapshot directory missing: {snap_dir}"
            )
    else:
        pointer = graph_root / "current"
        if pointer.is_file():
            try:
                snap_id = pointer.read_text(encoding="utf-8").strip()
            except OSError as e:
                raise OverlayCoherenceGraphAuditError(
                    f"cannot read current pointer: {pointer}: {e}"
                ) from e
            if not snap_id:
                raise OverlayCoherenceGraphAuditError(
                    f"empty current pointer: {pointer}"
                )
            if Path(snap_id).name != snap_id or snap_id in {".", ".."}:
                raise OverlayCoherenceGraphAuditError(
                    f"unsafe current snapshot id: {snap_id!r}"
                )
            snap_dir = (snapshots_dir / snap_id).resolve()
            if snap_dir.parent != snapshots_dir:
                raise OverlayCoherenceGraphAuditError(
                    f"current snapshot escapes snapshots directory: {snap_id!r}"
                )
            if not snap_dir.is_dir():
                raise OverlayCoherenceGraphAuditError(
                    f"current snapshot directory missing: {snap_dir}"
                )
        else:
            snap_dir = graph_root
            snap_id = ""
            if not (snap_dir / "relationships.parquet").is_file():
                raise OverlayCoherenceGraphAuditError(
                    f"no current pointer and no relationships.parquet under "
                    f"{graph_root}"
                )

    manifest_path = snap_dir / "manifest.json"
    if not manifest_path.is_file():
        raise OverlayCoherenceGraphAuditError(
            f"snapshot missing manifest.json: {manifest_path}"
        )
    try:
        manifest = strict_json_loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as e:
        raise OverlayCoherenceGraphAuditError(
            f"cannot read manifest: {manifest_path}: {e}"
        ) from e
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        raise OverlayCoherenceGraphAuditError(
            f"malformed manifest JSON: {manifest_path}: {e}"
        ) from e
    if not isinstance(manifest, dict):
        raise OverlayCoherenceGraphAuditError(
            f"manifest is not a JSON object: {manifest_path}"
        )
    return snap_dir, snap_id, manifest


def _load_table(
    snap_dir: Path, name: str, *, required: bool
) -> Optional[List[Dict[str, Any]]]:
    path = snap_dir / f"{name}.parquet"
    if not path.is_file():
        if required:
            raise OverlayCoherenceGraphAuditError(
                f"snapshot missing {name}.parquet: {path}"
            )
        return None
    try:
        import pandas as pd
    except ImportError as e:
        raise OverlayCoherenceGraphAuditError(
            f"pandas is required to read parquet: {e}"
        ) from e
    try:
        df = pd.read_parquet(path)
    except Exception as e:
        raise OverlayCoherenceGraphAuditError(
            f"cannot read {name}.parquet: {path}: {e}"
        ) from e
    try:
        return df.to_dict("records")
    except Exception as e:
        raise OverlayCoherenceGraphAuditError(
            f"cannot materialize {name} rows: {e}"
        ) from e


def read_only_fingerprint(graph_root: Path, snap_dir: Path) -> Dict[str, str]:
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
            raise OverlayCoherenceGraphAuditError(
                f"cannot fingerprint {path}: {e}"
            ) from e
        fingerprint[name] = digest.hexdigest()
    pointer = Path(graph_root) / "current"
    if pointer.is_file():
        try:
            fingerprint["current"] = hashlib.sha256(pointer.read_bytes()).hexdigest()
        except OSError as e:
            raise OverlayCoherenceGraphAuditError(
                f"cannot fingerprint {pointer}: {e}"
            ) from e
    else:
        fingerprint["current"] = "absent"
    snapshots_dir = Path(graph_root) / "snapshots"
    if snapshots_dir.is_dir():
        try:
            listing = sorted(p.name for p in snapshots_dir.iterdir())
        except OSError as e:
            raise OverlayCoherenceGraphAuditError(
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
    graph_root = Path(graph_root)
    snap_dir, snap_id, manifest = resolve_snapshot(graph_root, snapshot)
    before = read_only_fingerprint(graph_root, snap_dir)
    entities = _load_table(snap_dir, "entities", required=True) or []
    relationships = _load_table(snap_dir, "relationships", required=True) or []
    observations = _load_table(snap_dir, "call_observations", required=False)
    report = audit_rows(
        entities,
        relationships,
        observations,
        manifest,
        max_anomaly_samples=max_anomaly_samples,
        graph=str(graph_root),
        snapshot=snap_id or None,
    )
    after = read_only_fingerprint(graph_root, snap_dir)
    changed = sorted(name for name in before if before[name] != after.get(name))
    report["read_only_verification"] = {
        "verified": not changed,
        "method": "sha256 of graph inputs before and after the audit",
        "inputs": sorted(before),
        "changed_inputs": changed,
        "fingerprint": dict(sorted(after.items())),
    }
    if changed:
        sample_limit = max(0, max_anomaly_samples)
        report["ok"] = False
        report["status"] = "invalid"
        report["state"] = "invalid"
        report["mode"] = "invalid"
        report["classification"] = "invalid"
        report["n_anomalies"] = int(report["n_anomalies"]) + 1
        if len(report["anomalies"]) < sample_limit:
            report["anomalies"] = list(report["anomalies"]) + [
                {
                    "code": "read_only_violation",
                    "message": f"graph inputs changed during the audit: {changed}",
                }
            ]
        report["n_anomaly_samples"] = len(report["anomalies"])
        report["anomalies_truncated"] = (
            report["n_anomalies"] > report["n_anomaly_samples"]
        )
        report["n_violations"] = report["n_anomalies"]
        report["violations"] = list(report["anomalies"])
    return report


def audit_to_json(report: Mapping[str, Any]) -> str:
    return json.dumps(
        _json_safe(dict(report)),
        sort_keys=True,
        indent=2,
        ensure_ascii=True,
        allow_nan=False,
    ) + "\n"


def _validate_output_path(output: Path, graph_root: Path) -> None:
    try:
        resolved_output = Path(output).resolve()
        resolved_graph = Path(graph_root).resolve()
        resolved_output.relative_to(resolved_graph)
    except ValueError:
        return
    except OSError as e:
        raise OverlayCoherenceGraphAuditError(
            f"cannot resolve output path {output}: {e}"
        ) from e
    raise OverlayCoherenceGraphAuditError(
        f"--output must be outside the audited graph root: {output}"
    )


def format_report(report: Mapping[str, Any]) -> str:
    census = report.get("census") or {}
    lines = [
        "C overlay coherence audit (read-only; no compiler re-run)",
        f"  status={report.get('status')} ok={report.get('ok')} "
        f"mode={report.get('mode')}",
    ]
    if report.get("graph"):
        snap = report.get("snapshot") or ""
        suffix = f" @ {snap}" if snap else ""
        lines.append(f"  graph={report['graph']}{suffix}")
    lines.append(
        f"  enabled={census.get('enabled')} off={census.get('off')} "
        f"absent={census.get('absent')} anomalies={report.get('n_anomalies')}"
        + (
            f" (showing {report.get('n_anomaly_samples')})"
            if report.get("anomalies_truncated")
            else ""
        )
    )
    shared = report.get("shared") or {}
    if shared.get("compile_commands_digest"):
        lines.append(f"  digest={shared.get('compile_commands_digest')}")
    read_only = report.get("read_only_verification")
    if isinstance(read_only, Mapping):
        lines.append(
            f"  read_only_verified={read_only.get('verified')} "
            f"(changed={read_only.get('changed_inputs')})"
        )
    for anomaly in report.get("anomalies") or []:
        lines.append(
            f"  - [{anomaly.get('code')}]: {anomaly.get('message')}"
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
    except OverlayCoherenceGraphAuditError as e:
        print(f"c_overlay_coherence_graph_audit: {e}", file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError, KeyError) as e:
        print(f"c_overlay_coherence_graph_audit: {e}", file=sys.stderr)
        return 2

    text = audit_to_json(report)
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        except OSError as e:
            print(
                f"c_overlay_coherence_graph_audit: cannot write output: {e}",
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
