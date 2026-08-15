#!/usr/bin/env python
"""Bounded read-only snapshot history and structural snapshot diff.

Compares retained published snapshots under one managed graph root. This is
structural persisted-row comparison, not semantic equivalence: a modified
row means canonical persisted fields differ. Snapshot history is local and
bounded. Staging directories are notices, not published history.

Neither command creates ``.publish.lock``, reindexes, extracts, compiles,
publishes, retains, or repairs. A shared graph-root reader lease is held
across resolving ``current`` once, listing retained snapshots, loading
compared snapshots, and computing the complete response. Cooperating
publishers wait. Tools that ignore the lock are not protected.

CLI commands are strict by default and point at ``adopt-publication-lock``
when the lock is missing. ``--allow-unlocked-legacy`` is an explicit
read-only compatibility path for immutable pre-lock evidence; it never
creates the lock and provides no retention guarantee. MCP never exposes
that option.

Usage:
    graphrag-code snapshot-history --graph <root> [--limit 20] [--json]
    graphrag-code snapshot-diff --graph <root> --from <id|current> --to <id|current> [--max-items 50] [--json]
    python -m graphrag_code.snapshot_compare history --graph <root> --json
    python -m graphrag_code.snapshot_compare diff --graph <root> --from current --to current --json
    uv run python scripts/snapshot_compare.py history --graph <root>
"""
from __future__ import annotations

import argparse
import json
import math
import stat
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from graphrag_code.byog_graph import (
    ByogReaderLockError,
    _validate_managed_snapshot_layout,
    graph_read_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
)
from graphrag_code.byog_snapshot_graph_audit import (
    SnapshotGraphAuditError,
    _load_table,
    audit_rows,
    inventory_snapshot,
    resolve_snapshot,
)
from graphrag_code.persisted_graph_doctor import (
    PersistedGraphDoctorError,
    doctor_fingerprint,
)

DEFAULT_HISTORY_LIMIT = 20
HARD_MAX_HISTORY_LIMIT = 200
DEFAULT_DIFF_MAX_ITEMS = 50
HARD_MAX_DIFF_ITEMS = 500
MAX_PUBLICATION_NOTICE_NAMES = 20
DIFF_TABLES = ("entities", "relationships", "text_units", "call_observations")
CURRENT_REF = "current"
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_LEGACY_NOTICE = {
    "code": "legacy_unlocked",
    "kind": "notice",
    "message": (
        "read-only compatibility for immutable pre-lock evidence; "
        "there is no retention guarantee because no .publish.lock lease exists"
    ),
}


class SnapshotCompareError(Exception):
    """Expected compare failure. Default exit 2."""

    exit_code = 2


class SnapshotCompareIntegrityError(SnapshotCompareError):
    """Persisted integrity or concurrent mutation. Exit 1."""

    exit_code = 1


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotCompareError(f"graph root does not exist: {path}") from error
    except OSError as error:
        raise SnapshotCompareError(f"cannot inspect graph root {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotCompareError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotCompareError(f"graph root is not a real directory: {path}")
    return path.resolve()


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotCompareError(str(error)) from error
    if not managed:
        raise SnapshotCompareError(
            "legacy flat-parquet directory has no published snapshot history: "
            f"{root}"
        )


def _require_bound(name: str, value: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotCompareError(f"{name} must be an integer, got {value!r}")
    if value < minimum or value > maximum:
        raise SnapshotCompareError(
            f"{name} must be between {minimum} and {maximum}, got {value}"
        )
    return value


def _byte_sort(values: Iterable[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def _canonical(value: Any) -> Any:
    """Normalize a persisted scalar for honest JSON comparison.

    Parquet/pandas null and NaN become JSON null. Infinities and other
    non-finite values that cannot be represented honestly are rejected.
    """
    if value is None:
        return None
    try:
        import pandas as pd
    except ImportError:
        pass
    else:
        # ``pd.NaT`` is a datetime subclass, so this must precede the generic
        # date/datetime branch below.  Otherwise it becomes the JSON string
        # ``"NaT"`` instead of the promised JSON null.
        if value is pd.NA or value is pd.NaT:
            return None
        if isinstance(value, pd.Timestamp):
            return value.isoformat()
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value):
            return None
        if math.isinf(value):
            raise SnapshotCompareError(
                f"non-finite persisted value cannot be represented honestly: {value!r}"
            )
        return float(value)
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "surrogateescape")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise SnapshotCompareError(
                "persisted mapping keys must be strings for honest JSON comparison"
            )
        return {
            key: _canonical(value[key])
            for key in sorted(value, key=lambda item: item.encode("utf-8"))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if hasattr(value, "item") and callable(value.item) and not isinstance(value, (bytes, str)):
        try:
            if getattr(value, "shape", ()) == ():
                return _canonical(value.item())
        except Exception:
            pass
    if hasattr(value, "tolist") and callable(value.tolist) and not isinstance(
        value, (bytes, str, Mapping)
    ):
        try:
            converted = value.tolist()
        except Exception:
            converted = None
        else:
            if converted is not value:
                return _canonical(converted)
    raise SnapshotCompareError(
        f"persisted value has no honest JSON canonical form: {type(value).__name__}"
    )


def _canonical_equal(left: Any, right: Any) -> bool:
    """Compare canonical JSON values without Python's bool/int coercion."""
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping):
        return left.keys() == right.keys() and all(
            _canonical_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _canonical_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    return bool(left == right)


def _lock_error(error: ByogReaderLockError) -> SnapshotCompareError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotCompareError(f"{message}\n{_MISSING_LOCK_HINT}")
    return SnapshotCompareError(message)


def _resolve_current_once(
    root: Path,
) -> Tuple[Path, str, Dict[str, Any]]:
    try:
        snap_dir, snap_id, manifest = resolve_snapshot(root, None)
    except SnapshotGraphAuditError as error:
        raise SnapshotCompareError(str(error)) from error
    if snap_id is None or not isinstance(manifest, Mapping):
        raise SnapshotCompareError(f"graph has no published current snapshot: {root}")
    if not is_published_snapshot_id(snap_id):
        raise SnapshotCompareError(f"current snapshot id is not a published id: {snap_id!r}")
    return Path(snap_dir), str(snap_id), dict(manifest)


def _resolve_published(
    root: Path, snap_id: str
) -> Tuple[Path, str, Dict[str, Any]]:
    if not is_published_snapshot_id(snap_id):
        raise SnapshotCompareError(
            f"staging path is not a published snapshot: {snap_id!r}"
            if is_staging_snapshot_name(snap_id)
            else f"unsafe snapshot id: {snap_id!r}"
        )
    try:
        snap_dir, resolved, manifest = resolve_snapshot(root, snap_id)
    except SnapshotGraphAuditError as error:
        raise SnapshotCompareError(str(error)) from error
    if resolved is None or not isinstance(manifest, Mapping):
        raise SnapshotCompareError(f"snapshot is not a published directory: {snap_id!r}")
    return Path(snap_dir), str(resolved), dict(manifest)


def _parse_ref(ref: str) -> str:
    if not isinstance(ref, str) or not ref.strip():
        raise SnapshotCompareError("snapshot reference must be a nonempty string")
    value = ref.strip()
    if value == CURRENT_REF:
        return CURRENT_REF
    if (
        Path(value).name != value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise SnapshotCompareError(f"unsafe snapshot id: {value!r}")
    if is_staging_snapshot_name(value) or not is_published_snapshot_id(value):
        raise SnapshotCompareError(
            f"staging path is not a published snapshot: {value!r}"
            if is_staging_snapshot_name(value)
            else f"unsafe snapshot id: {value!r}"
        )
    return value


def _list_published_and_notices(root: Path) -> Tuple[List[str], List[Dict[str, Any]]]:
    snapshots = root / "snapshots"
    try:
        info = snapshots.lstat()
    except FileNotFoundError as error:
        raise SnapshotCompareError(f"snapshots directory missing: {snapshots}") from error
    except OSError as error:
        raise SnapshotCompareError(f"cannot inspect snapshots directory {snapshots}: {error}") from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotCompareError(f"unsafe symlinked snapshots directory: {snapshots}")
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotCompareError(f"snapshots path is not a real directory: {snapshots}")
    published: List[str] = []
    staging: List[str] = []
    try:
        entries = list(snapshots.iterdir())
    except OSError as error:
        raise SnapshotCompareError(f"cannot list {snapshots}: {error}") from error
    for path in sorted(entries, key=lambda item: item.name.encode("utf-8")):
        try:
            entry = path.lstat()
        except OSError as error:
            raise SnapshotCompareError(f"cannot inspect {path}: {error}") from error
        if stat.S_ISLNK(entry.st_mode):
            raise SnapshotCompareError(f"unsafe symlinked snapshot entry: {path}")
        name = path.name
        if is_staging_snapshot_name(name):
            if not stat.S_ISDIR(entry.st_mode):
                raise SnapshotCompareError(
                    f"staging path is not a directory: {path}"
                )
            staging.append(name)
            continue
        if is_published_snapshot_id(name) and stat.S_ISDIR(entry.st_mode):
            published.append(name)
            continue
        raise SnapshotCompareError(
            f"unexpected unsafe snapshots entry is not published history: {path}"
        )
    notices: List[Dict[str, Any]] = []
    if staging:
        shown = staging[:MAX_PUBLICATION_NOTICE_NAMES]
        notices.append(
            {
                "code": "staging_present",
                "kind": "notice",
                "message": (
                    "stable staging directories are a publication notice, "
                    "not published snapshot history"
                ),
                "n_staging": len(staging),
                "names": shown,
                "returned": len(shown),
                "truncated": len(staging) > len(shown),
            }
        )
    published.sort(key=lambda item: item.encode("utf-8"), reverse=True)
    return published, notices


def _load_tables(
    snap_dir: Path,
) -> Dict[str, List[Dict[str, Any]]]:
    try:
        present, sizes, symlinks, unexpected = inventory_snapshot(snap_dir)
        entities = _load_table(snap_dir, "entities", required=True) or []
        relationships = _load_table(snap_dir, "relationships", required=True) or []
        text_units = _load_table(snap_dir, "text_units", required=True) or []
        observations = _load_table(snap_dir, "call_observations", required=False)
    except SnapshotGraphAuditError as error:
        raise SnapshotCompareError(str(error)) from error
    if observations is None:
        observations = []
    return {
        "entities": list(entities),
        "relationships": list(relationships),
        "text_units": list(text_units),
        "call_observations": list(observations),
        "_inventory": (present, sizes, symlinks, unexpected),
    }


def _validate_envelope(
    snap_id: str,
    manifest: Mapping[str, Any],
    tables: Mapping[str, Any],
    *,
    graph: Path,
) -> None:
    present, sizes, symlinks, unexpected = tables["_inventory"]
    try:
        report = audit_rows(
            tables["entities"],
            tables["relationships"],
            tables["text_units"],
            tables["call_observations"] or None,
            manifest,
            snapshot_id=snap_id,
            present_files=present,
            file_sizes=sizes,
            symlinked_files=symlinks,
            unexpected_entries=unexpected,
            graph=str(graph),
            snapshot=snap_id,
        )
    except (TypeError, ValueError, KeyError) as error:
        raise SnapshotCompareError(f"unreadable snapshot envelope {snap_id}: {error}") from error
    if not report.get("ok"):
        raise SnapshotCompareIntegrityError(
            f"persisted snapshot envelope is not ok for {snap_id} "
            f"(status={report.get('status')} anomalies={report.get('n_anomalies')})"
        )


def _history_entry(
    snap_id: str,
    current_id: str,
    manifest: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "id": snap_id,
        "is_current": snap_id == current_id,
        "created_at": manifest.get("created_at"),
        "schema_version": manifest.get("schema_version"),
        "counts": _canonical(manifest.get("counts") or {}),
        "files": list(manifest.get("files") or []),
        "total_size_bytes": manifest.get("total_size_bytes"),
        "index_input_present": isinstance(manifest.get("index_input"), Mapping),
    }


def _fingerprint(graph_root: Path, snap_dirs: Sequence[Path]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    # ``current`` is commonly also one of the compared paths. Avoid hashing
    # that snapshot twice while retaining the original deterministic order.
    dirs = list(dict.fromkeys(Path(path) for path in snap_dirs)) or [graph_root]
    try:
        for snap_dir in dirs:
            part = doctor_fingerprint(graph_root, snap_dir)
            prefix = f"{Path(snap_dir).name}/"
            for key, value in part.items():
                if key.startswith("snapshot/"):
                    merged[prefix + key] = value
                else:
                    merged[key] = value
    except (PersistedGraphDoctorError, SnapshotGraphAuditError) as error:
        raise SnapshotCompareError(str(error)) from error
    return dict(sorted(merged.items()))


def _verify_fingerprints(before: Mapping[str, str], after: Mapping[str, str]) -> Dict[str, Any]:
    changed = sorted(name for name in before if before[name] != after.get(name))
    changed.extend(sorted(name for name in after if name not in before))
    changed = sorted(set(changed))
    verification = {
        "verified": not changed,
        "method": "sha256 of snapshot files, current, snapshots listing, and publish lock",
        "inputs": sorted(before),
        "changed_inputs": changed,
        "fingerprint": dict(sorted(after.items())),
    }
    if changed:
        raise SnapshotCompareIntegrityError(
            f"graph inputs changed during the read: {changed}"
        )
    return verification


def _verify_discovery_after_baseline(
    root: Path,
    *,
    current_dir: Path,
    current_id: str,
    published: Sequence[str],
) -> List[Dict[str, Any]]:
    """Re-read discovery inputs after the baseline fingerprint.

    Discovery is needed to know which bounded payloads to fingerprint. A
    lock-ignoring actor could mutate ``current`` or the snapshots listing
    between that preliminary read and the baseline. Requiring the second read
    to agree prevents a stale discovery result from being labelled verified.
    """
    try:
        pointer = root / "current"
        if pointer.is_symlink() or not pointer.is_file():
            raise SnapshotCompareError(f"unsafe or missing current pointer: {pointer}")
        checked_id = pointer.read_text(encoding="utf-8").strip()
        if not is_published_snapshot_id(checked_id):
            raise SnapshotCompareError(
                f"current snapshot id is not a published id: {checked_id!r}"
            )
        checked_published, notices = _list_published_and_notices(root)
    except (OSError, UnicodeDecodeError, SnapshotCompareError) as error:
        raise SnapshotCompareIntegrityError(
            f"graph discovery changed during the read: {error}"
        ) from error
    if (
        root / "snapshots" / checked_id != current_dir
        or checked_id != current_id
        or list(checked_published) != list(published)
    ):
        raise SnapshotCompareIntegrityError(
            "graph discovery changed during the read: current or snapshots listing"
        )
    return notices


def _legacy_fields(allow_unlocked_legacy: bool) -> Dict[str, Any]:
    extra: Dict[str, Any] = {}
    if allow_unlocked_legacy:
        extra["legacy_unlocked"] = True
        extra["retention_guarantee"] = False
    return extra


def _index_rows(table: str, rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise SnapshotCompareError(f"{table} row is not an object")
        if "id" not in row:
            raise SnapshotCompareError(f"{table} row is missing a nonempty string id")
        row_id = row["id"]
        if not isinstance(row_id, str) or row_id == "":
            raise SnapshotCompareError(
                f"{table} row id must be a nonempty string, got {row_id!r}"
            )
        if row_id in index:
            raise SnapshotCompareError(f"{table} has duplicate id {row_id!r}")
        non_string_keys = [key for key in row if not isinstance(key, str)]
        if non_string_keys:
            raise SnapshotCompareError(
                f"{table} row has non-string field name {non_string_keys[0]!r}"
            )
        fields = {key: _canonical(value) for key, value in row.items() if key != "id"}
        index[row_id] = fields
    return index


def _bounded_ids(ids: Sequence[str], max_items: int) -> Dict[str, Any]:
    values = list(ids)
    total = len(values)
    items = values[:max_items]
    return {
        "total": total,
        "items": items,
        "returned": len(items),
        "truncated": total > len(items),
    }


def _bounded_modified(
    items: Sequence[Dict[str, Any]], max_items: int
) -> Dict[str, Any]:
    values = list(items)
    total = len(values)
    shown = values[:max_items]
    return {
        "total": total,
        "items": shown,
        "returned": len(shown),
        "truncated": total > len(shown),
    }


def _diff_table(
    table: str,
    before_rows: Sequence[Mapping[str, Any]],
    after_rows: Sequence[Mapping[str, Any]],
    *,
    max_items: int,
) -> Dict[str, Any]:
    before = _index_rows(table, before_rows)
    after = _index_rows(table, after_rows)
    added = _byte_sort(set(after) - set(before))
    removed = _byte_sort(set(before) - set(after))
    modified: List[Dict[str, Any]] = []
    for row_id in _byte_sort(set(before) & set(after)):
        left = before[row_id]
        right = after[row_id]
        keys = set(left) | set(right)
        changed = [
            key
            for key in _byte_sort(keys)
            if key not in left
            or key not in right
            or not _canonical_equal(left[key], right[key])
        ]
        if changed:
            modified.append({"id": row_id, "changed_fields": changed})
    return {
        "before_rows": len(before_rows),
        "after_rows": len(after_rows),
        "added": _bounded_ids(added, max_items),
        "removed": _bounded_ids(removed, max_items),
        "modified": _bounded_modified(modified, max_items),
    }


def _manifest_key_summary(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> Dict[str, List[str]]:
    before_keys = {str(key) for key in before}
    after_keys = {str(key) for key in after}
    added = _byte_sort(after_keys - before_keys)
    removed = _byte_sort(before_keys - after_keys)
    changed = []
    for key in _byte_sort(before_keys & after_keys):
        if not _canonical_equal(_canonical(before[key]), _canonical(after[key])):
            changed.append(key)
    return {
        "added_keys": added,
        "removed_keys": removed,
        "changed_keys": changed,
    }


def _snapshot_history_unlocked(
    root: Path,
    *,
    limit: int,
    allow_unlocked_legacy: bool,
) -> Dict[str, Any]:
    _require_managed_graph(root)
    current_dir, current_id, _current_manifest = _resolve_current_once(root)
    published, _preliminary_notices = _list_published_and_notices(root)
    if current_id not in published:
        raise SnapshotCompareError(
            f"current snapshot is not a published snapshots/ directory: {current_id}"
        )
    returned_ids = published[:limit]
    # The snapshots listing fingerprint covers the exact total/history
    # membership. Only returned entries (plus current) are loaded, so hashing
    # every retained snapshot here would make a bounded history request perform
    # unbounded payload I/O.
    fingerprint_dirs = [root / "snapshots" / snap_id for snap_id in returned_ids]
    if current_dir not in fingerprint_dirs:
        fingerprint_dirs.append(current_dir)
    before = _fingerprint(root, fingerprint_dirs)
    notices = _verify_discovery_after_baseline(
        root,
        current_dir=current_dir,
        current_id=current_id,
        published=published,
    )
    if allow_unlocked_legacy:
        notices = list(notices) + [dict(_LEGACY_NOTICE)]
    entries: List[Dict[str, Any]] = []
    for snap_id in returned_ids:
        snap_dir, resolved, manifest = _resolve_published(root, snap_id)
        tables = _load_tables(snap_dir)
        _validate_envelope(resolved, manifest, tables, graph=root)
        entries.append(_history_entry(resolved, current_id, manifest))
    after = _fingerprint(root, fingerprint_dirs)
    verification = _verify_fingerprints(before, after)
    result: Dict[str, Any] = {
        "ok": True,
        "graph": str(root),
        "current": current_id,
        "snapshots": entries,
        "total": len(published),
        "returned": len(entries),
        "truncated": len(published) > len(entries),
        "publication_notices": notices,
        "read_only_verification": verification,
        "limits": {
            "limit": limit,
            "hard_max_limit": HARD_MAX_HISTORY_LIMIT,
        },
    }
    result.update(_legacy_fields(allow_unlocked_legacy))
    return result


def _snapshot_diff_unlocked(
    root: Path,
    from_ref: str,
    to_ref: str,
    *,
    max_items: int,
    allow_unlocked_legacy: bool,
) -> Dict[str, Any]:
    _require_managed_graph(root)
    from_token = _parse_ref(from_ref)
    to_token = _parse_ref(to_ref)
    current_dir, current_id, _current_manifest = _resolve_current_once(root)
    published, _preliminary_notices = _list_published_and_notices(root)
    from_id = current_id if from_token == CURRENT_REF else from_token
    to_id = current_id if to_token == CURRENT_REF else to_token
    from_dir, from_id, _preliminary_from_manifest = _resolve_published(root, from_id)
    to_dir, to_id, _preliminary_to_manifest = _resolve_published(root, to_id)
    before = _fingerprint(root, [from_dir, to_dir, current_dir])
    notices = _verify_discovery_after_baseline(
        root,
        current_dir=current_dir,
        current_id=current_id,
        published=published,
    )
    preliminary_from = (from_dir, from_id)
    preliminary_to = (to_dir, to_id)
    try:
        from_dir, from_id, from_manifest = _resolve_published(root, from_id)
        to_dir, to_id, to_manifest = _resolve_published(root, to_id)
    except SnapshotCompareError as error:
        raise SnapshotCompareIntegrityError(
            f"compared snapshot changed during the read: {error}"
        ) from error
    if (from_dir, from_id) != preliminary_from or (to_dir, to_id) != preliminary_to:
        raise SnapshotCompareIntegrityError(
            "compared snapshot identity changed during the read"
        )
    from_tables = _load_tables(from_dir)
    to_tables = _load_tables(to_dir)
    _validate_envelope(from_id, from_manifest, from_tables, graph=root)
    _validate_envelope(to_id, to_manifest, to_tables, graph=root)
    tables: Dict[str, Any] = {}
    added_total = 0
    removed_total = 0
    modified_total = 0
    for name in DIFF_TABLES:
        table = _diff_table(
            name,
            from_tables[name],
            to_tables[name],
            max_items=max_items,
        )
        tables[name] = table
        added_total += int(table["added"]["total"])
        removed_total += int(table["removed"]["total"])
        modified_total += int(table["modified"]["total"])
    manifest = _manifest_key_summary(from_manifest, to_manifest)
    after = _fingerprint(root, [from_dir, to_dir, current_dir])
    verification = _verify_fingerprints(before, after)
    logical = (
        added_total == 0
        and removed_total == 0
        and modified_total == 0
        and not manifest["added_keys"]
        and not manifest["removed_keys"]
        and not manifest["changed_keys"]
    )
    from_fp = {
        key: value
        for key, value in after.items()
        if key.startswith(f"{from_dir.name}/snapshot/")
    }
    to_fp = {
        key: value
        for key, value in after.items()
        if key.startswith(f"{to_dir.name}/snapshot/")
    }
    byte_identical = from_id == to_id or (
        {key.split("/", 1)[1] for key in from_fp} == {key.split("/", 1)[1] for key in to_fp}
        and all(
            from_fp[f"{from_dir.name}/{suffix}"] == to_fp[f"{to_dir.name}/{suffix}"]
            for suffix in {key.split("/", 1)[1] for key in from_fp}
        )
    )
    if allow_unlocked_legacy:
        notices = list(notices)
        notices.append(dict(_LEGACY_NOTICE))
    result: Dict[str, Any] = {
        "ok": True,
        "graph": str(root),
        "from_snapshot": from_id,
        "to_snapshot": to_id,
        "tables": tables,
        "totals": {
            "added": added_total,
            "removed": removed_total,
            "modified": modified_total,
        },
        "manifest": manifest,
        "identical": logical,
        "byte_identical": byte_identical,
        "limits": {
            "max_items": max_items,
            "hard_max_items": HARD_MAX_DIFF_ITEMS,
        },
        "read_only_verification": verification,
        "publication_notices": notices,
    }
    result.update(_legacy_fields(allow_unlocked_legacy))
    return result


def snapshot_history(
    graph: Path,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
    allow_unlocked_legacy: bool = False,
) -> Dict[str, Any]:
    """List published snapshots newest-first under one shared reader lease."""
    bound = _require_bound(
        "limit", limit, minimum=1, maximum=HARD_MAX_HISTORY_LIMIT
    )
    root = _resolve_graph_root(graph)
    try:
        with graph_read_lease(root, allow_unlocked_managed=allow_unlocked_legacy):
            return _snapshot_history_unlocked(
                root,
                limit=bound,
                allow_unlocked_legacy=allow_unlocked_legacy,
            )
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_diff(
    graph: Path,
    from_snapshot: str,
    to_snapshot: str = CURRENT_REF,
    *,
    max_items: int = DEFAULT_DIFF_MAX_ITEMS,
    allow_unlocked_legacy: bool = False,
) -> Dict[str, Any]:
    """Structurally diff two published snapshots under one shared reader lease."""
    bound = _require_bound(
        "max_items", max_items, minimum=1, maximum=HARD_MAX_DIFF_ITEMS
    )
    root = _resolve_graph_root(graph)
    try:
        with graph_read_lease(root, allow_unlocked_managed=allow_unlocked_legacy):
            return _snapshot_diff_unlocked(
                root,
                from_snapshot,
                to_snapshot,
                max_items=bound,
                allow_unlocked_legacy=allow_unlocked_legacy,
            )
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def result_to_json(result: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            result,
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )


def format_history(result: Mapping[str, Any]) -> str:
    lines = [
        "snapshot-history: "
        f"graph={result.get('graph')} current={result.get('current')} "
        f"total={result.get('total')} returned={result.get('returned')} "
        f"truncated={str(result.get('truncated')).lower()}"
    ]
    if result.get("legacy_unlocked"):
        lines.append("  legacy_unlocked=true retention_guarantee=false")
    for entry in result.get("snapshots") or []:
        marker = " (current)" if entry.get("is_current") else ""
        counts = entry.get("counts") or {}
        lines.append(
            f"  {entry.get('id')}{marker} created_at={entry.get('created_at')} "
            f"entities={counts.get('entities')} relationships={counts.get('relationships')}"
        )
    for notice in result.get("publication_notices") or []:
        lines.append(f"  notice[{notice.get('code')}]: {notice.get('message')}")
    return "\n".join(lines)


def format_diff(result: Mapping[str, Any]) -> str:
    totals = result.get("totals") or {}
    lines = [
        "snapshot-diff: "
        f"graph={result.get('graph')} from={result.get('from_snapshot')} "
        f"to={result.get('to_snapshot')} identical={str(result.get('identical')).lower()} "
        f"byte_identical={str(result.get('byte_identical')).lower()} "
        f"added={totals.get('added')} removed={totals.get('removed')} "
        f"modified={totals.get('modified')}"
    ]
    if result.get("legacy_unlocked"):
        lines.append("  legacy_unlocked=true retention_guarantee=false")
    for name in DIFF_TABLES:
        table = (result.get("tables") or {}).get(name) or {}
        added = (table.get("added") or {}).get("total", 0)
        removed = (table.get("removed") or {}).get("total", 0)
        modified = (table.get("modified") or {}).get("total", 0)
        lines.append(f"  {name}: +{added} -{removed} ~{modified}")
    manifest = result.get("manifest") or {}
    lines.append(
        "  manifest: "
        f"+{len(manifest.get('added_keys') or [])} "
        f"-{len(manifest.get('removed_keys') or [])} "
        f"~{len(manifest.get('changed_keys') or [])}"
    )
    for notice in result.get("publication_notices") or []:
        lines.append(f"  notice[{notice.get('code')}]: {notice.get('message')}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded read-only snapshot history and structural snapshot diff. "
            "Does not reindex, publish, repair, or create .publish.lock."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    history = sub.add_parser(
        "history",
        help="list published snapshots newest-first",
    )
    history.add_argument(
        "--graph",
        "-g",
        type=Path,
        required=True,
        help="Managed BYOG graph root, relative to cwd.",
    )
    history.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_HISTORY_LIMIT,
        help=(
            f"maximum published snapshots to return (default {DEFAULT_HISTORY_LIMIT}, "
            f"hard max {HARD_MAX_HISTORY_LIMIT})"
        ),
    )
    history.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    history.add_argument(
        "--allow-unlocked-legacy",
        action="store_true",
        help=(
            "Explicit read-only compatibility for immutable pre-lock evidence. "
            "Never creates .publish.lock and provides no retention guarantee."
        ),
    )

    diff = sub.add_parser(
        "diff",
        help="structurally compare two published snapshots",
    )
    diff.add_argument(
        "--graph",
        "-g",
        type=Path,
        required=True,
        help="Managed BYOG graph root, relative to cwd.",
    )
    diff.add_argument(
        "--from",
        dest="from_snapshot",
        required=True,
        help="published snapshot id or 'current'",
    )
    diff.add_argument(
        "--to",
        dest="to_snapshot",
        required=True,
        help="published snapshot id or 'current'",
    )
    diff.add_argument(
        "--max-items",
        type=int,
        default=DEFAULT_DIFF_MAX_ITEMS,
        help=(
            f"sample cap per added/removed/modified category per table "
            f"(default {DEFAULT_DIFF_MAX_ITEMS}, hard max {HARD_MAX_DIFF_ITEMS})"
        ),
    )
    diff.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    diff.add_argument(
        "--allow-unlocked-legacy",
        action="store_true",
        help=(
            "Explicit read-only compatibility for immutable pre-lock evidence. "
            "Never creates .publish.lock and provides no retention guarantee."
        ),
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "history":
            result = snapshot_history(
                args.graph,
                limit=args.limit,
                allow_unlocked_legacy=bool(args.allow_unlocked_legacy),
            )
            text = format_history(result)
        else:
            result = snapshot_diff(
                args.graph,
                args.from_snapshot,
                args.to_snapshot,
                max_items=args.max_items,
                allow_unlocked_legacy=bool(args.allow_unlocked_legacy),
            )
            text = format_diff(result)
    except SnapshotCompareError as error:
        print(f"snapshot-compare: {error}", file=sys.stderr)
        return error.exit_code
    except (SnapshotGraphAuditError, PersistedGraphDoctorError) as error:
        print(f"snapshot-compare: {error}", file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-compare: {error}", file=sys.stderr)
        return 2

    if args.json:
        sys.stdout.write(result_to_json(result))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
