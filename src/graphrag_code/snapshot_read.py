#!/usr/bin/env python
"""Shared retained-snapshot read scope for query, context-pack, doctor, and MCP.

Holds one shared ``.publish.lock`` reader lease across reference validation,
snapshot resolution, parquet load, and the complete response. This is not
activation: ``current`` is never modified. An explicit published id is
resolved beneath ``<graph>/snapshots/<id>`` without reading ``current``.
``current`` is resolved exactly once when that selector is used.

CLI default (omitted ``--snapshot``) keeps the existing compatibility path
for legacy flat parquet and pre-lock managed graphs. That path never
creates ``.publish.lock`` and has no retention guarantee. MCP is always
strict. Advisory locks protect only cooperating processes.

Reuse :func:`resolve_snapshot` for path safety. Do not add a second resolver.
"""
from __future__ import annotations

import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional

from graphrag_code.byog_graph import (
    ByogGraph,
    ByogReaderLockError,
    _validate_managed_snapshot_layout,
    graph_read_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
)
from graphrag_code.byog_snapshot_graph_audit import (
    SnapshotGraphAuditError,
    resolve_snapshot,
)

CURRENT_REF = "current"
CORE_INPUTS = (
    "entities.parquet",
    "relationships.parquet",
    "text_units.parquet",
    "manifest.json",
)


class SnapshotReadError(Exception):
    """Expected retained-snapshot read failure. Default exit 2."""

    exit_code = 2


class SnapshotReadIntegrityError(SnapshotReadError):
    """Selected snapshot is unreadable after a successful resolve. Exit 1."""

    exit_code = 1


@dataclass(frozen=True)
class RetainedSnapshotScope:
    """Pinned retained snapshot for one logical read. Valid only inside the lease."""

    graph_root: Path
    snap_dir: Path
    snap_id: Optional[str]
    manifest: Optional[Dict[str, Any]]

    def load_graph(self) -> ByogGraph:
        try:
            return ByogGraph._from_resolved_base(self.snap_dir)
        except FileNotFoundError as error:
            raise SnapshotReadIntegrityError(
                f"pinned snapshot is no longer readable: {error}"
            ) from error
        except OSError as error:
            raise SnapshotReadIntegrityError(
                f"pinned snapshot is no longer readable: {error}"
            ) from error
        except ValueError as error:
            raise SnapshotReadIntegrityError(
                f"pinned snapshot is malformed or unreadable: {error}"
            ) from error


def parse_snapshot_ref(ref: Optional[str]) -> Optional[str]:
    """Return None (omitted), ``current``, or a canonical published id."""
    if ref is None:
        return None
    if not isinstance(ref, str) or ref.strip() == "":
        raise SnapshotReadError("snapshot reference must be a nonempty string")
    value = ref.strip()
    if value != ref:
        raise SnapshotReadError(
            f"snapshot reference must not contain leading or trailing whitespace: {ref!r}"
        )
    if value == CURRENT_REF:
        return CURRENT_REF
    if (
        Path(value).name != value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise SnapshotReadError(f"unsafe snapshot id: {value!r}")
    if is_staging_snapshot_name(value) or not is_published_snapshot_id(value):
        raise SnapshotReadError(
            f"staging path is not a published snapshot: {value!r}"
            if is_staging_snapshot_name(value)
            else f"unsafe snapshot id: {value!r}"
        )
    return value


def _require_regular_core_inputs(snap_dir: Path) -> None:
    for name in CORE_INPUTS:
        path = snap_dir / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            if name == "text_units.parquet":
                continue
            if name == "manifest.json":
                # Legacy flat directories have no snapshot envelope.
                continue
            raise SnapshotReadError(f"snapshot missing {name}: {path}") from None
        except OSError as error:
            raise SnapshotReadError(f"cannot inspect {path}: {error}") from error
        if stat.S_ISLNK(info.st_mode):
            raise SnapshotReadError(f"unsafe symlinked core snapshot input: {path}")
        if name.endswith(".parquet") and not stat.S_ISREG(info.st_mode):
            raise SnapshotReadError(f"core snapshot input is not a regular file: {path}")


def _select_snapshot(
    root: Path,
    parsed: Optional[str],
) -> RetainedSnapshotScope:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotReadError(str(error)) from error

    if parsed is None:
        if not managed:
            _require_regular_core_inputs(root)
            return RetainedSnapshotScope(
                graph_root=root,
                snap_dir=root,
                snap_id=None,
                manifest=None,
            )
        try:
            snap_dir, snap_id, manifest = resolve_snapshot(root, None)
        except SnapshotGraphAuditError as error:
            raise SnapshotReadError(str(error)) from error
        if snap_id is None or not isinstance(manifest, Mapping):
            raise SnapshotReadError(f"graph has no published current snapshot: {root}")
        _require_regular_core_inputs(Path(snap_dir))
        return RetainedSnapshotScope(
            graph_root=root,
            snap_dir=Path(snap_dir),
            snap_id=str(snap_id),
            manifest=dict(manifest),
        )

    if not managed:
        if parsed == CURRENT_REF:
            raise SnapshotReadError(
                "legacy flat-parquet directory has no current snapshot pointer: "
                f"{root}"
            )
        raise SnapshotReadError(
            "legacy flat-parquet directory has no retained snapshot history: "
            f"{root}"
        )

    if parsed == CURRENT_REF:
        try:
            snap_dir, snap_id, manifest = resolve_snapshot(root, None)
        except SnapshotGraphAuditError as error:
            raise SnapshotReadError(str(error)) from error
        if snap_id is None or not isinstance(manifest, Mapping):
            raise SnapshotReadError(f"graph has no published current snapshot: {root}")
        _require_regular_core_inputs(Path(snap_dir))
        return RetainedSnapshotScope(
            graph_root=root,
            snap_dir=Path(snap_dir),
            snap_id=str(snap_id),
            manifest=dict(manifest),
        )

    try:
        snap_dir, snap_id, manifest = resolve_snapshot(root, parsed)
    except SnapshotGraphAuditError as error:
        raise SnapshotReadError(str(error)) from error
    if snap_id is None or not isinstance(manifest, Mapping):
        raise SnapshotReadError(f"snapshot is not a published directory: {parsed!r}")
    _require_regular_core_inputs(Path(snap_dir))
    return RetainedSnapshotScope(
        graph_root=root,
        snap_dir=Path(snap_dir),
        snap_id=str(snap_id),
        manifest=dict(manifest),
    )


@contextmanager
def retained_snapshot_read(
    graph_root: Path,
    snapshot_ref: Optional[str] = None,
    *,
    allow_unlocked_managed: bool = False,
) -> Iterator[RetainedSnapshotScope]:
    """Hold one shared reader lease and pin one retained snapshot.

    ``snapshot_ref`` is omitted (``None``), ``current``, or a published id.
    The lease is held for the lifetime of the context. Never creates
    ``.publish.lock``. Never takes a nested shared lease.
    """
    root = Path(graph_root)
    parsed = parse_snapshot_ref(snapshot_ref)
    # Only the omitted-selector CLI compatibility path may read a pre-lock
    # managed graph without retention protection.  An explicit selector
    # promises a retained-snapshot read and therefore requires the existing
    # publication lock just like MCP does.
    allow_unlocked = allow_unlocked_managed and parsed is None
    try:
        with graph_read_lease(root, allow_unlocked_managed=allow_unlocked):
            yield _select_snapshot(root, parsed)
    except ByogReaderLockError as error:
        raise SnapshotReadError(str(error)) from error
