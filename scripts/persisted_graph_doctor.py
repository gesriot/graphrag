#!/usr/bin/env python
"""Read-only persisted-integrity doctor for a BYOG graph.

Selects one snapshot, validates its envelope, then runs every applicable
overlay contract against that same loaded snapshot. Does not compare the
graph with a fresh extractor, invoke a compiler, repair data, or acquire
the publication lock.

Exit codes:
  0 – every applicable persisted contract is valid
  1 – persisted integrity violation or detected concurrent mutation
  2 – unsafe path, ambiguous auto-indexer, malformed JSON, unreadable
      parquet, missing required input, or invalid output path

Usage:
    uv run python scripts/persisted_graph_doctor.py --graph byog_mini_game --indexer python
    uv run python scripts/persisted_graph_doctor.py --graph byog_cjson --indexer c --json
    uv run python scripts/graphrag_code.py doctor --graph byog_cjson --indexer auto
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from byog_graph import (  # type: ignore
    PUBLICATION_LOCK_NAME,
    is_staging_snapshot_name,
)
from byog_snapshot_graph_audit import (  # type: ignore
    SnapshotGraphAuditError,
    _json_safe,
    _load_table,
    _validate_output_path,
    inventory_snapshot,
    read_only_fingerprint,
    resolve_snapshot,
)
from persisted_graph_integrity import (  # type: ignore
    AmbiguousIndexerError,
    C_COMPONENT_ORDER,
    LIMITATIONS,
    validate_persisted_graph_integrity,
)

AUDIT_MODE = "persisted_graph_integrity"


class PersistedGraphDoctorError(Exception):
    """Raised when the graph/snapshot cannot be read honestly."""


def _lock_fingerprint(graph_root: Path) -> str:
    lock = Path(graph_root) / PUBLICATION_LOCK_NAME
    try:
        before = lock.lstat()
    except FileNotFoundError:
        return "absent"
    except OSError as e:
        raise PersistedGraphDoctorError(
            f"cannot inspect publication lock {lock}: {e}"
        ) from e
    if stat.S_ISLNK(before.st_mode):
        raise PersistedGraphDoctorError(
            f"unsafe symlinked publication lock: {lock}"
        )
    if not stat.S_ISREG(before.st_mode):
        return "other"

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(lock, flags)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise PersistedGraphDoctorError(
                f"unsafe symlinked publication lock: {lock}"
            ) from e
        if e.errno == errno.ENOENT:
            return "absent"
        raise PersistedGraphDoctorError(
            f"cannot fingerprint publication lock {lock}: {e}"
        ) from e
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise PersistedGraphDoctorError(
                f"publication lock changed to a non-regular file: {lock}"
            )
        if getattr(os, "O_NOFOLLOW", 0) == 0 and (
            opened.st_dev != before.st_dev or opened.st_ino != before.st_ino
        ):
            raise PersistedGraphDoctorError(
                f"publication lock changed while opening it: {lock}"
            )
        digest = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=True) as handle:
            fd = -1
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as e:
        raise PersistedGraphDoctorError(
            f"cannot fingerprint publication lock {lock}: {e}"
        ) from e
    finally:
        if fd >= 0:
            os.close(fd)
    return "regular:" + digest.hexdigest()


def doctor_fingerprint(graph_root: Path, snap_dir: Path) -> Dict[str, str]:
    fingerprint = read_only_fingerprint(graph_root, snap_dir)
    fingerprint["graph/publish_lock"] = _lock_fingerprint(graph_root)
    return fingerprint


def staging_census(graph_root: Path) -> List[str]:
    snapshots = Path(graph_root) / "snapshots"
    if snapshots.is_symlink() or not snapshots.is_dir():
        return []
    names: List[str] = []
    try:
        for path in sorted(snapshots.iterdir(), key=lambda item: item.name):
            if (
                not path.is_symlink()
                and path.is_dir()
                and is_staging_snapshot_name(path.name)
            ):
                names.append(path.name)
    except OSError as e:
        raise PersistedGraphDoctorError(f"cannot list {snapshots}: {e}") from e
    return names


def audit_rows(
    entities: Any,
    relationships: Any,
    text_units: Any,
    call_observations: Any,
    manifest: Optional[Mapping[str, Any]],
    *,
    indexer: str,
    snapshot_id: Optional[str] = None,
    present_files: Optional[List[str]] = None,
    file_sizes: Optional[Mapping[str, int]] = None,
    symlinked_files: Optional[List[str]] = None,
    unexpected_entries: Optional[List[str]] = None,
    max_anomaly_samples: int = 40,
    graph: Optional[str] = None,
    snapshot: Optional[str] = None,
) -> Dict[str, Any]:
    """Pure dataframe/row API. Never mutates inputs or reads the filesystem."""
    result = validate_persisted_graph_integrity(
        entities,
        relationships,
        text_units,
        call_observations,
        dict(manifest) if manifest is not None else None,
        indexer=indexer,
        snapshot_id=snapshot_id,
        present_files=present_files,
        file_sizes=file_sizes,
        symlinked_files=symlinked_files,
        unexpected_entries=unexpected_entries,
        max_anomaly_samples=max_anomaly_samples,
    )
    out: Dict[str, Any] = {
        "audit_mode": AUDIT_MODE,
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "state": str(result["state"]),
        "classification": str(result["classification"]),
        "indexer": result["indexer"],
        "indexer_resolution": dict(result["indexer_resolution"]),
        "snapshot_integrity": dict(result["snapshot_integrity"]),
        "components": dict(result["components"]),
        "failed_components": list(result["failed_components"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
        "limitations": list(LIMITATIONS),
    }
    if graph is not None:
        out["graph"] = graph
    if snapshot is not None:
        out["snapshot"] = snapshot
    return out


def _apply_mutation(
    report: Dict[str, Any],
    changed: List[str],
    *,
    max_anomaly_samples: int,
) -> Dict[str, Any]:
    sample_limit = max(0, max_anomaly_samples)
    report["ok"] = False
    report["status"] = "invalid"
    report["state"] = "invalid"
    report["classification"] = "invalid"
    report["n_anomalies"] = int(report["n_anomalies"]) + 1
    if "read_only_violation" not in report["failed_components"]:
        report["failed_components"] = list(report["failed_components"]) + [
            "read_only_violation"
        ]
    if len(report["anomalies"]) < sample_limit:
        report["anomalies"] = list(report["anomalies"]) + [
            {
                "code": "read_only_violation",
                "component": "read_only_verification",
                "message": f"graph inputs changed during the audit: {changed}",
            }
        ]
    report["n_anomaly_samples"] = len(report["anomalies"])
    report["anomalies_truncated"] = report["n_anomalies"] > report["n_anomaly_samples"]
    return report


def audit_graph_root(
    graph_root: Path,
    *,
    indexer: str,
    snapshot: Optional[str] = None,
    max_anomaly_samples: int = 40,
) -> Dict[str, Any]:
    graph_root = Path(graph_root)
    snap_dir, snap_id, _ = resolve_snapshot(graph_root, snapshot)
    before = doctor_fingerprint(graph_root, snap_dir)

    confirmed_dir, confirmed_id, manifest = resolve_snapshot(graph_root, snapshot)
    selection_changed = confirmed_dir != snap_dir or confirmed_id != snap_id
    if selection_changed and snap_id is not None:
        _, _, manifest = resolve_snapshot(graph_root, snap_id)

    present_files, file_sizes, symlinked_files, unexpected_entries = inventory_snapshot(
        snap_dir
    )
    entities = _load_table(snap_dir, "entities", required=True) or []
    relationships = _load_table(snap_dir, "relationships", required=True) or []
    text_units = _load_table(snap_dir, "text_units", required=True) or []
    observations = _load_table(snap_dir, "call_observations", required=False)
    notices: List[Dict[str, Any]] = []
    staging = staging_census(graph_root)
    if staging:
        notices.append(
            {
                "code": "staging_present",
                "kind": "notice",
                "message": (
                    "stable staging directories are a publication notice, "
                    "not proven persisted corruption"
                ),
                "n_staging": len(staging),
                "names": staging,
            }
        )
    report = audit_rows(
        entities,
        relationships,
        text_units,
        observations,
        manifest,
        indexer=indexer,
        snapshot_id=snap_id,
        present_files=present_files,
        file_sizes=file_sizes,
        symlinked_files=symlinked_files,
        unexpected_entries=unexpected_entries,
        max_anomaly_samples=max_anomaly_samples,
        graph=str(graph_root),
        snapshot=snap_id,
    )
    report["publication_notices"] = notices
    after = doctor_fingerprint(graph_root, snap_dir)
    changed = sorted(name for name in before if before[name] != after.get(name))
    extra_after = sorted(name for name in after if name not in before)
    changed.extend(extra_after)
    if selection_changed:
        changed.append("graph/current_selection")
    changed = sorted(set(changed))
    report["read_only_verification"] = {
        "verified": not changed,
        "method": "sha256 of snapshot files, current, snapshots listing, and publish lock",
        "inputs": sorted(before),
        "changed_inputs": changed,
        "fingerprint": dict(sorted(after.items())),
    }
    if changed:
        report = _apply_mutation(
            report, changed, max_anomaly_samples=max_anomaly_samples
        )
    return report


def audit_to_json(report: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            _json_safe(dict(report)),
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )


def format_report(report: Mapping[str, Any]) -> str:
    envelope = report.get("snapshot_integrity") or {}
    lines = [
        "BYOG persisted-integrity doctor (read-only; no extractor or compiler)",
        f"  status={report.get('status')} ok={report.get('ok')} "
        f"indexer={report.get('indexer')} "
        f"resolution={(report.get('indexer_resolution') or {}).get('reason')}",
    ]
    if report.get("graph"):
        snap = report.get("snapshot") or ""
        suffix = f" @ {snap}" if snap else ""
        lines.append(f"  graph={report['graph']}{suffix}")
    lines.append(
        f"  snapshot_envelope: status={envelope.get('status')} "
        f"ok={envelope.get('ok')} anomalies={envelope.get('n_anomalies')}"
    )
    for name in C_COMPONENT_ORDER:
        component = (report.get("components") or {}).get(name)
        if not isinstance(component, Mapping):
            continue
        lines.append(
            f"  {name}: status={component.get('status')} "
            f"ok={component.get('ok')} mode={component.get('mode')} "
            f"anomalies={component.get('n_anomalies')}"
        )
    for notice in report.get("publication_notices") or []:
        lines.append(
            f"  notice[{notice.get('code')}]: {notice.get('message')} "
            f"n={notice.get('n_staging')}"
        )
    read_only = report.get("read_only_verification")
    if isinstance(read_only, Mapping):
        lines.append(
            f"  read_only_verified={read_only.get('verified')} "
            f"(changed={read_only.get('changed_inputs')})"
        )
    failed = report.get("failed_components") or []
    if failed:
        lines.append(f"  failed_components={failed}")
    lines.append(
        f"  anomalies={report.get('n_anomalies')}"
        + (
            f" (showing {report.get('n_anomaly_samples')})"
            if report.get("anomalies_truncated")
            else ""
        )
    )
    for anomaly in report.get("anomalies") or []:
        lines.append(
            f"  - [{anomaly.get('component')}/{anomaly.get('code')}]: "
            f"{anomaly.get('message')}"
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
        "--indexer",
        type=str,
        required=True,
        choices=("python", "c", "auto"),
        help="python, c, or auto (fail closed if persisted evidence is ambiguous)",
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
            indexer=args.indexer,
            snapshot=args.snapshot,
            max_anomaly_samples=args.max_anomaly_samples,
        )
    except AmbiguousIndexerError as e:
        print(f"persisted_graph_doctor: {e}", file=sys.stderr)
        return 2
    except (PersistedGraphDoctorError, SnapshotGraphAuditError) as e:
        print(f"persisted_graph_doctor: {e}", file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as e:
        print(f"persisted_graph_doctor: {e}", file=sys.stderr)
        return 2

    text = audit_to_json(report)
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        except OSError as e:
            print(f"persisted_graph_doctor: cannot write output: {e}", file=sys.stderr)
            return 2
    if args.json:
        sys.stdout.write(text)
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
