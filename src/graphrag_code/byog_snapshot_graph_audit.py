#!/usr/bin/env python
"""Read-only snapshot-envelope audit for a persisted BYOG graph.

Validates that the selected snapshot directory, manifest core fields, and
loaded table census still agree with ``publish_byog_snapshot()``. This is
language-independent: it applies to every BYOG indexer.

Does **not** invoke an extractor, compiler, or Clang; read source packages
or compile_commands.json; reconstruct overlays; reindex, repair, publish,
rename, or delete snapshots; update current; or compare source_root,
git_commit, or created_at with the current host.

Managed graphs with an existing ``.publish.lock`` are audited under a shared
reader lease. Immutable pre-lock evidence uses an explicitly unleased,
fingerprinted compatibility read; the audit never creates the lock.

Exit codes:
  0 – valid snapshot envelope
  1 – persisted integrity violations
  2 – unsafe path, malformed JSON, unreadable parquet, missing required
      input, schema/load error, or invalid output path

Usage:
    uv run python scripts/byog_snapshot_graph_audit.py --graph byog_mini_game
    uv run python scripts/byog_snapshot_graph_audit.py --graph byog_cjson \\
        --snapshot 20260808-153310-5b68f044 --output snapshot_audit.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple


from graphrag_code.byog_graph import (  # type: ignore
    ByogReaderLockError,
    graph_read_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
)
from graphrag_code.byog_snapshot_integrity import (  # type: ignore
    LIMITATIONS,
    validate_persisted_byog_snapshot,
)

AUDIT_MODE = "byog_snapshot_envelope_integrity"


class SnapshotGraphAuditError(Exception):
    """Raised when the graph/snapshot/manifest cannot be read honestly."""


def strict_json_loads(text: str) -> Any:
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
    text_units: Any,
    call_observations: Any = None,
    manifest: Optional[Mapping[str, Any]] = None,
    *,
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
    result = validate_persisted_byog_snapshot(
        entities,
        relationships,
        text_units,
        call_observations,
        dict(manifest) if manifest is not None else None,
        snapshot_id=snapshot_id,
        present_files=present_files,
        file_sizes=file_sizes,
        max_anomaly_samples=max_anomaly_samples,
        symlinked_files=symlinked_files,
        unexpected_entries=unexpected_entries,
    )
    out: Dict[str, Any] = {
        "audit_mode": AUDIT_MODE,
        "ok": bool(result["ok"]),
        "status": str(result["status"]),
        "state": str(result["state"]),
        "mode": str(result["mode"]),
        "classification": str(result["classification"]),
        "directory_identity": str(result["directory_identity"]),
        "n_anomalies": int(result["n_anomalies"]),
        "n_anomaly_samples": int(result["n_anomaly_samples"]),
        "anomalies_truncated": bool(result["anomalies_truncated"]),
        "anomalies": list(result["anomalies"]),
        "n_violations": int(result["n_anomalies"]),
        "violations": list(result["anomalies"]),
        "census": dict(result["census"]),
        "expected_files": list(result["expected_files"]),
        "limitations": list(LIMITATIONS),
    }
    if graph is not None:
        out["graph"] = graph
    if snapshot is not None:
        out["snapshot"] = snapshot
    return out


def resolve_snapshot(
    graph_root: Path, snapshot: Optional[str] = None
) -> Tuple[Path, Optional[str], Dict[str, Any]]:
    graph_root = Path(graph_root)
    if not graph_root.exists():
        raise SnapshotGraphAuditError(f"graph root does not exist: {graph_root}")
    if not graph_root.is_dir():
        raise SnapshotGraphAuditError(f"graph root is not a directory: {graph_root}")
    try:
        resolved_graph = graph_root.resolve(strict=True)
    except OSError as e:
        raise SnapshotGraphAuditError(f"cannot resolve graph root {graph_root}: {e}") from e
    snapshots_path = graph_root / "snapshots"
    if snapshot is not None:
        snap_id = snapshot.strip()
        if not snap_id:
            raise SnapshotGraphAuditError("empty --snapshot id")
        if (
            Path(snap_id).name != snap_id
            or snap_id in {".", ".."}
            or "/" in snap_id
            or "\\" in snap_id
            or "\x00" in snap_id
        ):
            raise SnapshotGraphAuditError(f"unsafe snapshot id: {snap_id!r}")
        if is_staging_snapshot_name(snap_id) or not is_published_snapshot_id(snap_id):
            raise SnapshotGraphAuditError(
                f"staging path is not a published snapshot: {snap_id!r}"
            )
        if snapshots_path.is_symlink():
            raise SnapshotGraphAuditError(
                f"unsafe symlinked snapshots directory: {snapshots_path}"
            )
        if not snapshots_path.is_dir():
            raise SnapshotGraphAuditError(
                f"snapshots directory missing: {snapshots_path}"
            )
        snapshots_dir = snapshots_path.resolve()
        snap_path = snapshots_path / snap_id
        if snap_path.is_symlink():
            raise SnapshotGraphAuditError(
                f"unsafe symlinked snapshot directory: {snap_path}"
            )
        snap_dir = snap_path.resolve()
        if snap_dir.parent != snapshots_dir:
            raise SnapshotGraphAuditError(
                f"snapshot escapes snapshots directory: {snap_id!r}"
            )
        if not snap_dir.is_dir():
            raise SnapshotGraphAuditError(f"snapshot directory missing: {snap_dir}")
    else:
        pointer = graph_root / "current"
        if pointer.is_symlink():
            raise SnapshotGraphAuditError(
                f"unsafe symlinked current pointer: {pointer}"
            )
        if pointer.is_file():
            try:
                snap_id = pointer.read_text(encoding="utf-8").strip()
            except OSError as e:
                raise SnapshotGraphAuditError(
                    f"cannot read current pointer: {pointer}: {e}"
                ) from e
            if not snap_id:
                raise SnapshotGraphAuditError(f"empty current pointer: {pointer}")
            if (
                Path(snap_id).name != snap_id
                or snap_id in {".", ".."}
                or "/" in snap_id
                or "\\" in snap_id
                or "\x00" in snap_id
            ):
                raise SnapshotGraphAuditError(
                    f"unsafe current snapshot id: {snap_id!r}"
                )
            if is_staging_snapshot_name(snap_id) or not is_published_snapshot_id(snap_id):
                raise SnapshotGraphAuditError(
                    f"current names a staging path, not a published snapshot: {snap_id!r}"
                )
            if snapshots_path.is_symlink():
                raise SnapshotGraphAuditError(
                    f"unsafe symlinked snapshots directory: {snapshots_path}"
                )
            if not snapshots_path.is_dir():
                raise SnapshotGraphAuditError(
                    f"snapshots directory missing: {snapshots_path}"
                )
            snapshots_dir = snapshots_path.resolve()
            snap_path = snapshots_path / snap_id
            if snap_path.is_symlink():
                raise SnapshotGraphAuditError(
                    f"unsafe symlinked snapshot directory: {snap_path}"
                )
            snap_dir = snap_path.resolve()
            if snap_dir.parent != snapshots_dir:
                raise SnapshotGraphAuditError(
                    f"current snapshot escapes snapshots directory: {snap_id!r}"
                )
            if not snap_dir.is_dir():
                raise SnapshotGraphAuditError(
                    f"current snapshot directory missing: {snap_dir}"
                )
        else:
            snap_dir = resolved_graph
            snap_id = None
            entities_path = snap_dir / "entities.parquet"
            if entities_path.is_symlink():
                raise SnapshotGraphAuditError(
                    f"unsafe symlinked core snapshot input: {entities_path}"
                )
            if not entities_path.is_file():
                raise SnapshotGraphAuditError(
                    f"no current pointer and no entities.parquet under {graph_root}"
                )

    manifest_path = snap_dir / "manifest.json"
    if manifest_path.is_symlink():
        raise SnapshotGraphAuditError(
            f"unsafe symlinked core snapshot input: {manifest_path}"
        )
    if not manifest_path.is_file():
        raise SnapshotGraphAuditError(
            f"snapshot missing manifest.json: {manifest_path}"
        )
    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise SnapshotGraphAuditError(
            f"cannot read manifest: {manifest_path}: {e}"
        ) from e
    try:
        manifest = strict_json_loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        raise SnapshotGraphAuditError(
            f"malformed manifest JSON: {manifest_path}: {e}"
        ) from e
    if not isinstance(manifest, dict):
        raise SnapshotGraphAuditError(
            f"manifest is not a JSON object: {manifest_path}"
        )
    return snap_dir, snap_id, manifest


def _load_table(
    snap_dir: Path, name: str, *, required: bool
) -> Optional[List[Dict[str, Any]]]:
    path = snap_dir / f"{name}.parquet"
    if path.is_symlink():
        raise SnapshotGraphAuditError(
            f"unsafe symlinked core snapshot input: {path}"
        )
    if not path.is_file():
        if required:
            raise SnapshotGraphAuditError(f"snapshot missing {name}.parquet: {path}")
        return None
    try:
        import pandas as pd
    except ImportError as e:
        raise SnapshotGraphAuditError(f"pandas is required to read parquet: {e}") from e
    try:
        df = pd.read_parquet(path)
    except Exception as e:
        raise SnapshotGraphAuditError(
            f"cannot read {name}.parquet: {path}: {e}"
        ) from e
    try:
        return df.to_dict("records")
    except Exception as e:
        raise SnapshotGraphAuditError(f"cannot materialize {name} rows: {e}") from e


def inventory_snapshot(
    snap_dir: Path,
) -> Tuple[List[str], Dict[str, int], List[str], List[str]]:
    present: List[str] = []
    sizes: Dict[str, int] = {}
    symlinks: List[str] = []
    other_entries: List[str] = []
    try:
        entries = list(Path(snap_dir).iterdir())
    except OSError as e:
        raise SnapshotGraphAuditError(f"cannot list snapshot directory {snap_dir}: {e}") from e
    for path in sorted(entries, key=lambda item: item.name):
        try:
            if path.is_symlink():
                symlinks.append(path.name)
                continue
            if path.is_file():
                present.append(path.name)
                sizes[path.name] = path.stat().st_size
            else:
                other_entries.append(path.name)
        except OSError as e:
            raise SnapshotGraphAuditError(f"cannot stat {path}: {e}") from e
    return present, sizes, symlinks, other_entries


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as e:
        raise SnapshotGraphAuditError(f"cannot fingerprint {path}: {e}") from e
    return digest.hexdigest()


def read_only_fingerprint(graph_root: Path, snap_dir: Path) -> Dict[str, str]:
    fingerprint: Dict[str, str] = {}
    try:
        entries = list(Path(snap_dir).iterdir())
    except OSError as e:
        raise SnapshotGraphAuditError(
            f"cannot list snapshot directory {snap_dir}: {e}"
        ) from e
    listing: List[str] = []
    for path in sorted(entries, key=lambda item: item.name):
        try:
            if path.is_symlink():
                listing.append(f"symlink\t{path.name}\t{path.readlink()}")
                continue
            if path.is_dir():
                listing.append(f"directory\t{path.name}")
                continue
            if not path.is_file():
                listing.append(f"other\t{path.name}")
                continue
            listing.append(f"file\t{path.name}")
        except OSError as e:
            raise SnapshotGraphAuditError(f"cannot stat {path}: {e}") from e
        fingerprint[f"snapshot/{path.name}"] = _sha256_file(path)
    fingerprint["snapshot/listing"] = hashlib.sha256(
        "\n".join(listing).encode("utf-8")
    ).hexdigest()
    pointer = Path(graph_root) / "current"
    if pointer.is_symlink():
        try:
            target = str(pointer.readlink())
        except OSError as e:
            raise SnapshotGraphAuditError(f"cannot read symlink {pointer}: {e}") from e
        fingerprint["graph/current"] = "symlink:" + hashlib.sha256(
            target.encode("utf-8")
        ).hexdigest()
    elif pointer.is_file():
        fingerprint["graph/current"] = _sha256_file(pointer)
    else:
        fingerprint["graph/current"] = "absent"
    snapshots_dir = Path(graph_root) / "snapshots"
    if snapshots_dir.is_symlink():
        try:
            target = str(snapshots_dir.readlink())
        except OSError as e:
            raise SnapshotGraphAuditError(
                f"cannot read symlink {snapshots_dir}: {e}"
            ) from e
        fingerprint["graph/snapshots_listing"] = "symlink:" + hashlib.sha256(
            target.encode("utf-8")
        ).hexdigest()
    elif snapshots_dir.is_dir():
        try:
            snapshots_listing = []
            for path in sorted(snapshots_dir.iterdir(), key=lambda item: item.name):
                if path.is_symlink():
                    kind = f"symlink:{path.readlink()}"
                elif path.is_dir():
                    kind = "directory"
                elif path.is_file():
                    kind = "file"
                else:
                    kind = "other"
                snapshots_listing.append(f"{kind}\t{path.name}")
        except OSError as e:
            raise SnapshotGraphAuditError(f"cannot list {snapshots_dir}: {e}") from e
        fingerprint["graph/snapshots_listing"] = hashlib.sha256(
            "\n".join(snapshots_listing).encode("utf-8")
        ).hexdigest()
    else:
        fingerprint["graph/snapshots_listing"] = "absent"
    return fingerprint


def audit_graph_root(
    graph_root: Path,
    *,
    snapshot: Optional[str] = None,
    max_anomaly_samples: int = 40,
) -> Dict[str, Any]:
    graph_root = Path(graph_root)
    try:
        with graph_read_lease(graph_root, allow_unlocked_managed=True):
            return _audit_graph_root_unlocked(
                graph_root,
                snapshot=snapshot,
                max_anomaly_samples=max_anomaly_samples,
            )
    except ByogReaderLockError as exc:
        raise SnapshotGraphAuditError(str(exc)) from exc


def _audit_graph_root_unlocked(
    graph_root: Path,
    *,
    snapshot: Optional[str] = None,
    max_anomaly_samples: int = 40,
) -> Dict[str, Any]:
    snap_dir, snap_id, _ = resolve_snapshot(graph_root, snapshot)
    before = read_only_fingerprint(graph_root, snap_dir)

    # Resolve and read the manifest again after the initial fingerprint.  The
    # first resolution identifies which directory must be fingerprinted, but
    # its manifest/current reads happen before that fingerprint.  Rechecking
    # here makes those reads part of the protected interval and closes the
    # window where ``current`` could otherwise switch snapshots just before
    # the initial fingerprint without appearing in the before/after diff.
    confirmed_dir, confirmed_id, manifest = resolve_snapshot(graph_root, snapshot)
    selection_changed = confirmed_dir != snap_dir or confirmed_id != snap_id
    if selection_changed:
        # Continue auditing the directory selected at entry.  An explicit
        # snapshot lookup re-reads its manifest without following a changed
        # current pointer; the report is invalidated below in every case.
        if snap_id is not None:
            _, _, manifest = resolve_snapshot(graph_root, snap_id)

    present_files, file_sizes, symlinked_files, unexpected_entries = inventory_snapshot(
        snap_dir
    )
    entities = _load_table(snap_dir, "entities", required=True) or []
    relationships = _load_table(snap_dir, "relationships", required=True) or []
    text_units = _load_table(snap_dir, "text_units", required=True) or []
    observations = _load_table(snap_dir, "call_observations", required=False)
    report = audit_rows(
        entities,
        relationships,
        text_units,
        observations,
        manifest,
        snapshot_id=snap_id,
        present_files=present_files,
        file_sizes=file_sizes,
        symlinked_files=symlinked_files,
        unexpected_entries=unexpected_entries,
        max_anomaly_samples=max_anomaly_samples,
        graph=str(graph_root),
        snapshot=snap_id,
    )
    after = read_only_fingerprint(graph_root, snap_dir)
    changed = sorted(name for name in before if before[name] != after.get(name))
    extra_after = sorted(name for name in after if name not in before)
    changed.extend(extra_after)
    if selection_changed:
        changed.append("graph/current_selection")
    changed = sorted(set(changed))
    report["read_only_verification"] = {
        "verified": not changed,
        "method": "sha256 of snapshot regular files, current, and snapshots listing",
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


def _validate_output_path(output: Path, graph_root: Path) -> None:
    try:
        resolved_output = Path(output).resolve()
        resolved_graph = Path(graph_root).resolve()
        resolved_output.relative_to(resolved_graph)
    except ValueError:
        return
    except OSError as e:
        raise SnapshotGraphAuditError(f"cannot resolve output path {output}: {e}") from e
    raise SnapshotGraphAuditError(
        f"--output must be outside the audited graph root: {output}"
    )


def format_report(report: Mapping[str, Any]) -> str:
    census = report.get("census") or {}
    lines = [
        "BYOG snapshot envelope audit (read-only; no extractor or compiler)",
        f"  status={report.get('status')} ok={report.get('ok')} "
        f"mode={report.get('mode')} "
        f"directory_identity={report.get('directory_identity')}",
    ]
    if report.get("graph"):
        snap = report.get("snapshot") or ""
        suffix = f" @ {snap}" if snap else ""
        lines.append(f"  graph={report['graph']}{suffix}")
    lines.append(
        f"  entities={census.get('entities')} "
        f"relationships={census.get('relationships')} "
        f"text_units={census.get('text_units')} "
        f"call_observations={census.get('call_observations')} "
        f"anomalies={report.get('n_anomalies')}"
        + (
            f" (showing {report.get('n_anomaly_samples')})"
            if report.get("anomalies_truncated")
            else ""
        )
    )
    read_only = report.get("read_only_verification")
    if isinstance(read_only, Mapping):
        lines.append(
            f"  read_only_verified={read_only.get('verified')} "
            f"(changed={read_only.get('changed_inputs')})"
        )
    for anomaly in report.get("anomalies") or []:
        lines.append(f"  - [{anomaly.get('code')}]: {anomaly.get('message')}")
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
    except SnapshotGraphAuditError as e:
        print(f"byog_snapshot_graph_audit: {e}", file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError, KeyError) as e:
        print(f"byog_snapshot_graph_audit: {e}", file=sys.stderr)
        return 2

    text = audit_to_json(report)
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        except OSError as e:
            print(
                f"byog_snapshot_graph_audit: cannot write output: {e}",
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
