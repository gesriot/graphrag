#!/usr/bin/env python
"""Read-only inventory of snapshots/.staging-* entries.

``snapshot-staging`` reports a bounded structural listing of private
publisher staging names under a managed graph. It does not delete,
rename, repair, quarantine, publish, activate, pin, prune, or
age-classify anything.

Publishers construct ``snapshots/.staging-*`` outside the publication
lock and take the exclusive lock only for promotion. Cooperating
publishers also hold a dedicated advisory writer lease on
``snapshots/.staging-<id>/.staging-writer.lock`` for the staging-write
interval. The shared graph lease does not cover that private
construction window and does not make staging contents immutable.
This command inspects the snapshots listing and writer-lock metadata
twice under one shared existing-lock lease and exits 1 when the scans
disagree. Two-scan agreement is bounded change detection, not a
liveness lease over a staging writer. Observed writer-lease contention
means only that a cooperating process held the private lease at that
scan. A successful probe means only that the lease was not held at
that instant. Neither proves writer death or cleanup eligibility.
A stable listing is not proof that a writer is dead. Ownership is
always unknown. Cleanup is not implemented here; ``cleanup_eligible``
stays false. A separate read-only ``snapshot-staging-cleanup-plan``
command may classify leftovers without deleting them. No age, mtime,
PID, process, host, or timeout heuristic is used.

The command requires an already-adopted regular ``.publish.lock`` and
never creates, truncates, rewrites, chmods, or replaces that lock. It
never creates or changes ``.snapshot-pins.json``, ``current``, snapshot
payloads, or staging entries. Advisory locks protect only cooperating
processes. MCP stays exactly 16 read-only tools; this command is
CLI-only.

Usage:
    graphrag-code snapshot-staging --graph <root> [--json]
    python -m graphrag_code.snapshot_staging --graph <root> [--json]
    uv run python scripts/snapshot_staging.py --graph <root> [--json]
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from graphrag_code.byog_graph import (
    PUBLICATION_LOCK_NAME,
    STAGING_NAME_PREFIX,
    STAGING_WRITER_LOCK_NAME,
    ByogPublicationLockError,
    ByogReaderLockError,
    StagingWriterLeaseError,
    StagingWriterLockUnsafe,
    _validate_managed_snapshot_layout,
    graph_read_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
    probe_staging_writer_lease,
)

SCHEMA_VERSION = 2
MAX_STAGING_ENTRIES = 64
MAX_TOP_LEVEL_ENTRIES = 64
MAX_PUBLISHED_SNAPSHOTS = 4096
MAX_CURRENT_BYTES = 512
REQUIRED_PAYLOAD_FILES = (
    "manifest.json",
    "entities.parquet",
    "relationships.parquet",
    "text_units.parquet",
)
OPTIONAL_PAYLOAD_FILES = (
    "call_observations.parquet",
    "settings.yaml",
)
EXPECTED_PAYLOAD_FILES = frozenset(REQUIRED_PAYLOAD_FILES + OPTIONAL_PAYLOAD_FILES)
PROTOCOL_FILES = frozenset({STAGING_WRITER_LOCK_NAME})
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_STAGING_REVISION_KEYS = (
    "current",
    "published_snapshots",
    "schema_version",
    "staging_entries",
)
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "ownership_unknown",
        "kind": "notice",
        "message": (
            "A stable staging listing is not proof that a writer is dead. "
            "Ownership is unknown. This inventory does not use wall-clock "
            "age, mtimes, PID probing, process discovery, host identity, "
            "or guessed timeouts to infer ownership."
        ),
    },
    {
        "code": "cleanup_not_supported",
        "kind": "notice",
        "message": (
            "Cleanup is not implemented. This command does not delete, "
            "rename, quarantine, repair, or age-classify staging entries. "
            "No backup, recovery, or distributed lease is claimed."
        ),
    },
    {
        "code": "staging_not_leased",
        "kind": "notice",
        "message": (
            "Publishers construct snapshots/.staging-* outside the "
            "graph-root publication lock and take that lock only for "
            "promotion. The shared graph lease is not a liveness lease "
            "over a staging writer. The private staging-writer lease is "
            "a separate advisory file. Two-scan agreement is bounded "
            "change detection, not proof that staging is idle."
        ),
    },
    {
        "code": "writer_lease_not_ownership",
        "kind": "notice",
        "message": (
            "Observed writer-lease contention means only that a "
            "cooperating process held snapshots/.staging-<id>/"
            ".staging-writer.lock at that scan. A successful "
            "acquire-and-release probe means only that the lease was "
            "not held at that instant. The persistent lock file is "
            "protocol metadata, not proof of ownership or writer death. "
            "Missing lock metadata is legacy/unverifiable. Cleanup is "
            "not implemented."
        ),
    },
)


class SnapshotStagingError(Exception):
    """Expected staging-inventory failure. Default exit 2."""

    exit_code = 2


class SnapshotStagingIntegrityError(SnapshotStagingError):
    """Persisted integrity, symlink, or concurrent mutation. Exit 1."""

    exit_code = 1


def _entry_kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return "other"


def _notice(code: str, message: str, **fields: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"code": code, "kind": "notice", "message": message}
    payload.update(fields)
    return payload


def canonical_staging_revision_payload(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Inventory inputs bound by ``staging_revision``. Presentation fields excluded."""
    payload: Dict[str, Any] = {}
    for key in _STAGING_REVISION_KEYS:
        if key not in result:
            raise SnapshotStagingError(
                f"staging inventory is missing revision input {key!r}"
            )
        payload[key] = result[key]
    return payload


def canonical_staging_revision_text(result: Mapping[str, Any]) -> str:
    """Canonical JSON of the revision inputs. Documented hash input.

    Compact UTF-8 JSON with sorted keys, no trailing newline:
    ``json.dumps(..., sort_keys=True, separators=(",", ":"),
    ensure_ascii=True, allow_nan=False)``.
    """
    return json.dumps(
        canonical_staging_revision_payload(result),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def staging_revision_of(result: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        canonical_staging_revision_text(result).encode("utf-8")
    ).hexdigest()
    return "sha256:" + digest


def _json_ready_state(value: Any) -> Any:
    """Convert the internal consistency token into JSON-ready values."""
    if isinstance(value, tuple):
        return [_json_ready_state(item) for item in value]
    if isinstance(value, list):
        return [_json_ready_state(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_ready_state(item) for key, item in value.items()}
    return value


def canonical_staging_state_revision_payload(
    consistency: Mapping[str, Any],
) -> Dict[str, Any]:
    """Internal two-scan state bound by ``staging_state_revision``.

    This is not the public inventory. It includes identities that the
    public ``staging_entries`` omit so an inode replacement is visible
    even when public fields stay equivalent.
    """
    required = (
        "current",
        "current_identity",
        "lock_identity",
        "published",
        "staging",
    )
    payload: Dict[str, Any] = {}
    for key in required:
        if key not in consistency:
            raise SnapshotStagingError(
                f"staging consistency state is missing revision input {key!r}"
            )
        payload[key] = _json_ready_state(consistency[key])
    return payload


def canonical_staging_state_revision_text(consistency: Mapping[str, Any]) -> str:
    """Canonical JSON of the internal consistency token. Documented hash input.

    Compact UTF-8 JSON with sorted keys, no trailing newline:
    ``json.dumps(..., sort_keys=True, separators=(",", ":"),
    ensure_ascii=True, allow_nan=False)``.
    """
    return json.dumps(
        canonical_staging_state_revision_payload(consistency),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def staging_state_revision_of(consistency: Mapping[str, Any]) -> str:
    """SHA-256 over the internal two-scan consistency token.

    Binds current identity/content, publication-lock identity, the
    published snapshot listing, each staging entry's name/type/dev/
    inode/mode/mtime/size, top-level child identities and metadata, and
    writer-lock identity, type, presence, and observed lease state.
    """
    digest = hashlib.sha256(
        canonical_staging_state_revision_text(consistency).encode("utf-8")
    ).hexdigest()
    return "sha256:" + digest


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


def format_result(result: Mapping[str, Any]) -> str:
    names = [entry.get("name") for entry in (result.get("staging_entries") or [])]
    rendered = ",".join(str(name) for name in names if name)
    suffix = f" names={rendered}" if rendered else ""
    states = [
        str(entry.get("writer_lease_state") or "unverifiable")
        for entry in (result.get("staging_entries") or [])
    ]
    lease = f" writer_lease={','.join(states)}" if states else ""
    return (
        "snapshot-staging: "
        f"graph={result.get('graph')} "
        f"current={result.get('current')} "
        f"published={result.get('published_count')} "
        f"staging={result.get('staging_count')} "
        f"staging_revision={result.get('staging_revision')}"
        f"{lease}"
        f"{suffix}"
    )


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotStagingError(f"graph root does not exist: {path}") from error
    except OSError as error:
        raise SnapshotStagingError(
            f"cannot inspect graph root {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotStagingError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotStagingError(f"graph root is not a real directory: {path}")
    return path.resolve()


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotStagingError(str(error)) from error
    if not managed:
        raise SnapshotStagingError(
            "legacy flat-parquet directory has no managed snapshot staging "
            f"inventory to report: {root}"
        )


def _lock_error(error: Exception) -> SnapshotStagingError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotStagingError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotStagingIntegrityError(message)
    return SnapshotStagingError(message)


def _lock_identity(root: Path) -> Tuple[int, int, int, int]:
    lock = root / PUBLICATION_LOCK_NAME
    try:
        info = lock.lstat()
    except FileNotFoundError as error:
        raise SnapshotStagingIntegrityError(
            f"publication lock disappeared during staging inventory: {lock}"
        ) from error
    except OSError as error:
        raise SnapshotStagingIntegrityError(
            f"cannot inspect publication lock {lock}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotStagingIntegrityError(
            f"unsafe symlinked publication lock is unsupported: {lock}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotStagingIntegrityError(
            f"publication lock is not a regular file: {lock}"
        )
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_current(root: Path) -> Tuple[str, Tuple[int, int, int, int]]:
    """Read the current pointer through a verified ``O_NOFOLLOW`` fd."""
    path = root / "current"
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotStagingIntegrityError(
            f"current pointer disappeared during staging inventory: {path}"
        ) from error
    except OSError as error:
        raise SnapshotStagingIntegrityError(
            f"cannot inspect current pointer {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotStagingIntegrityError(
            f"unsafe symlinked current pointer is unsupported: {path}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotStagingIntegrityError(
            f"current pointer is not a regular file: {path}"
        )
    if not hasattr(os, "O_NOFOLLOW"):
        raise SnapshotStagingError(
            "safe no-follow current-pointer reads are unsupported on "
            f"this platform: {sys.platform!r}"
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(path), flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT}:
            raise SnapshotStagingIntegrityError(
                f"current pointer changed or became unsafe while opening it: {path}"
            ) from error
        raise SnapshotStagingError(
            f"cannot safely open current pointer {path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current_path = path.lstat()
        except OSError as error:
            raise SnapshotStagingIntegrityError(
                f"current pointer changed while opening it: {path}"
            ) from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current_path.st_mode)
            or not stat.S_ISREG(current_path.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current_path.st_dev, current_path.st_ino)
            != (opened.st_dev, opened.st_ino)
        ):
            raise SnapshotStagingIntegrityError(
                f"current pointer changed or became unsafe while opening it: {path}"
            )
        chunks: List[bytes] = []
        total = 0
        while total <= MAX_CURRENT_BYTES:
            chunk = os.read(fd, min(128, MAX_CURRENT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_CURRENT_BYTES:
            raise SnapshotStagingError(
                f"current pointer exceeds bound {MAX_CURRENT_BYTES} bytes: {path}"
            )
        after_fd = os.fstat(fd)
        try:
            after_path = path.lstat()
        except OSError as error:
            raise SnapshotStagingIntegrityError(
                f"current pointer changed while it was read: {path}"
            ) from error
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if (
            (after_fd.st_dev, after_fd.st_ino, after_fd.st_size, after_fd.st_mtime_ns)
            != identity
            or stat.S_ISLNK(after_path.st_mode)
            or not stat.S_ISREG(after_path.st_mode)
            or (after_path.st_dev, after_path.st_ino, after_path.st_size, after_path.st_mtime_ns)
            != identity
        ):
            raise SnapshotStagingIntegrityError(
                f"current pointer changed while it was read: {path}"
            )
    finally:
        os.close(fd)
    try:
        current_id = data.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise SnapshotStagingError(
            f"current pointer is not valid UTF-8: {path}"
        ) from error
    if not is_published_snapshot_id(current_id):
        raise SnapshotStagingIntegrityError(
            f"current snapshot id is not a published id: {current_id!r}"
        )
    return current_id, identity


def _safe_directory_entries(
    path: Path, *, max_entries: int, label: str
) -> Tuple[os.stat_result, List[Tuple[str, os.stat_result]]]:
    """List one real directory through an ``O_NOFOLLOW`` descriptor.

    The count is enforced while iterating, rather than after an unbounded
    ``list(...)``. Descriptor-relative ``scandir`` also prevents a pathname
    swap from redirecting the scan through a symlink after ``lstat``.
    """
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.scandir not in getattr(os, "supports_fd", set())
    ):
        raise SnapshotStagingError(
            "safe descriptor-relative directory scanning is unsupported on "
            f"this platform: {sys.platform!r}"
        )
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotStagingIntegrityError(
            f"{label} disappeared during staging inventory: {path}"
        ) from error
    except OSError as error:
        raise SnapshotStagingIntegrityError(
            f"cannot inspect {label} {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotStagingIntegrityError(f"unsafe symlinked {label}: {path}")
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotStagingError(f"{label} is not a real directory: {path}")

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(path), flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise SnapshotStagingIntegrityError(
                f"{label} changed or became unsafe while opening it: {path}"
            ) from error
        raise SnapshotStagingError(f"cannot safely open {label} {path}: {error}") from error

    try:
        opened = os.fstat(fd)
        try:
            current = path.lstat()
        except OSError as error:
            raise SnapshotStagingIntegrityError(
                f"{label} changed while opening it: {path}"
            ) from error
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise SnapshotStagingIntegrityError(
                f"{label} changed or became unsafe while opening it: {path}"
            )

        entries: List[Tuple[str, os.stat_result]] = []
        try:
            with os.scandir(fd) as iterator:
                for entry in iterator:
                    if len(entries) >= max_entries:
                        raise SnapshotStagingError(
                            f"{label} entry count exceeds bound {max_entries}: {path}"
                        )
                    try:
                        entry_info = entry.stat(follow_symlinks=False)
                    except OSError as error:
                        raise SnapshotStagingIntegrityError(
                            f"cannot inspect {label} child {path / entry.name}: {error}"
                        ) from error
                    entries.append((entry.name, entry_info))
        except SnapshotStagingError:
            raise
        except OSError as error:
            raise SnapshotStagingIntegrityError(
                f"cannot list {label} {path}: {error}"
            ) from error

        after_fd = os.fstat(fd)
        try:
            after_path = path.lstat()
        except OSError as error:
            raise SnapshotStagingIntegrityError(
                f"{label} changed while it was listed: {path}"
            ) from error
        if (
            stat.S_ISLNK(after_path.st_mode)
            or not stat.S_ISDIR(after_path.st_mode)
            or (after_path.st_dev, after_path.st_ino)
            != (opened.st_dev, opened.st_ino)
            or (after_fd.st_dev, after_fd.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise SnapshotStagingIntegrityError(
                f"{label} changed or became unsafe while it was listed: {path}"
            )
        if (
            after_fd.st_mode,
            after_fd.st_mtime_ns,
            after_fd.st_size,
        ) != (opened.st_mode, opened.st_mtime_ns, opened.st_size):
            raise SnapshotStagingIntegrityError(
                f"{label} metadata changed while it was listed: {path}"
            )
        entries.sort(key=lambda item: item[0].encode("utf-8"))
        return opened, entries
    finally:
        os.close(fd)


def _list_snapshot_entries(root: Path) -> Tuple[List[str], List[Path]]:
    snapshots = root / "snapshots"
    _info, entries = _safe_directory_entries(
        snapshots,
        max_entries=MAX_PUBLISHED_SNAPSHOTS + MAX_STAGING_ENTRIES,
        label="snapshots directory",
    )
    published: List[str] = []
    staging: List[Path] = []
    for name, entry in entries:
        path = snapshots / name
        if stat.S_ISLNK(entry.st_mode):
            if is_staging_snapshot_name(name):
                raise SnapshotStagingIntegrityError(
                    f"unsafe symlinked staging entry: {path}"
                )
            raise SnapshotStagingIntegrityError(
                f"unsafe symlinked snapshot entry: {path}"
            )
        if is_staging_snapshot_name(name):
            staging.append(path)
            if len(staging) > MAX_STAGING_ENTRIES:
                raise SnapshotStagingError(
                    f"staging entry count exceeds bound {MAX_STAGING_ENTRIES}"
                )
            continue
        if is_published_snapshot_id(name) and stat.S_ISDIR(entry.st_mode):
            published.append(name)
            if len(published) > MAX_PUBLISHED_SNAPSHOTS:
                raise SnapshotStagingError(
                    "published snapshot count exceeds bound "
                    f"{MAX_PUBLISHED_SNAPSHOTS}"
                )
            continue
        raise SnapshotStagingError(
            f"unexpected unsafe snapshots entry is not published history: {path}"
        )
    return published, staging


def _collect_staging_children(
    path: Path,
) -> Tuple[
    os.stat_result,
    List[Dict[str, Any]],
    List[Dict[str, Any]],
    Dict[str, bool],
    List[Dict[str, Any]],
]:
    """List one staging directory's top-level children. Does not probe leases."""
    opened, children = _safe_directory_entries(
        path,
        max_entries=MAX_TOP_LEVEL_ENTRIES,
        label="staging directory",
    )
    children_reported: List[Dict[str, Any]] = []
    children_token: List[Dict[str, Any]] = []
    notices: List[Dict[str, Any]] = []
    present = {name: False for name in EXPECTED_PAYLOAD_FILES}
    for child_name, child_info in children:
        child = path / child_name
        if stat.S_ISLNK(child_info.st_mode):
            raise SnapshotStagingIntegrityError(
                f"unsafe symlinked staging child: {child}"
            )
        child_kind = _entry_kind(child_info.st_mode)
        if child_kind == "directory":
            notices.append(
                _notice(
                    "nested_directory",
                    "nested directory reported without recursion",
                    name=child.name,
                )
            )
        elif child_kind in {"fifo", "socket", "device", "other"}:
            notices.append(
                _notice(
                    "non_regular_entry",
                    "non-regular top-level entry is reported and is not opened",
                    name=child.name,
                    entry_kind=child_kind,
                )
            )
        elif (
            child_kind == "file"
            and child.name not in EXPECTED_PAYLOAD_FILES
            and child.name not in PROTOCOL_FILES
        ):
            notices.append(
                _notice(
                    "unexpected_top_level_entry",
                    "top-level file is outside the expected payload set",
                    name=child.name,
                )
            )
        if child.name == STAGING_WRITER_LOCK_NAME and child_kind != "file":
            raise SnapshotStagingIntegrityError(
                f"unsafe non-regular staging writer lock: {child}"
            )
        if child_kind == "file" and child.name in present:
            present[child.name] = True
        children_reported.append(
            {
                "name": child.name,
                "entry_kind": child_kind,
                "size_bytes": child_info.st_size if child_kind == "file" else None,
            }
        )
        children_token.append(
            {
                "name": child.name,
                "entry_kind": child_kind,
                "dev": child_info.st_dev,
                "ino": child_info.st_ino,
                "mode": child_info.st_mode,
                "mtime_ns": child_info.st_mtime_ns,
                "size": child_info.st_size,
            }
        )
    return opened, children_reported, children_token, present, notices


def staging_structure_token(path: Path) -> Dict[str, Any]:
    """Directory and child identities without probing the writer lease.

    Apply revalidation uses this after claiming the existing writer lock.
    A same-process lease probe would not be an honest observation.
    """
    try:
        info = path.lstat()
    except OSError as error:
        raise SnapshotStagingIntegrityError(f"cannot inspect {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotStagingIntegrityError(f"unsafe symlinked staging entry: {path}")
    kind = _entry_kind(info.st_mode)
    children_token: List[Dict[str, Any]] = []
    if kind == "directory":
        info, _reported, children_token, _present, _notices = _collect_staging_children(
            path
        )
    return {
        "name": path.name,
        "entry_kind": kind,
        "dev": info.st_dev,
        "ino": info.st_ino,
        "mode": info.st_mode,
        "mtime_ns": info.st_mtime_ns,
        "size": info.st_size,
        "children": children_token,
    }


def _inspect_staging_entry(path: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    try:
        info = path.lstat()
    except OSError as error:
        raise SnapshotStagingIntegrityError(f"cannot inspect {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotStagingIntegrityError(f"unsafe symlinked staging entry: {path}")
    kind = _entry_kind(info.st_mode)
    suffix = path.name[len(STAGING_NAME_PREFIX) :]
    name_valid = is_published_snapshot_id(suffix)
    candidate = suffix if name_valid else None
    notices: List[Dict[str, Any]] = []
    children_reported: List[Dict[str, Any]] = []
    children_token: List[Dict[str, Any]] = []
    present = {name: False for name in EXPECTED_PAYLOAD_FILES}

    if kind != "directory":
        notices.append(
            _notice(
                "staging_entry_not_directory",
                "staging entry is not a real directory; top-level payload "
                "shape is not inspected",
                name=path.name,
                entry_kind=kind,
            )
        )
    else:
        (
            info,
            children_reported,
            children_token,
            present,
            child_notices,
        ) = _collect_staging_children(path)
        notices.extend(child_notices)

    if not name_valid:
        notices.append(
            _notice(
                "name_not_canonical",
                "staging suffix is not a canonical published snapshot id",
                name=path.name,
            )
        )
    complete = bool(
        kind == "directory"
        and name_valid
        and present["manifest.json"]
        and present["entities.parquet"]
        and present["relationships.parquet"]
        and present["text_units.parquet"]
    )
    if kind == "directory" and name_valid and not complete:
        notices.append(
            _notice(
                "incomplete_payload_shape",
                "expected top-level payload files are not all present; "
                "this is not a validity, publication, or cleanup judgment",
                name=path.name,
            )
        )
    lease = _classify_writer_lease(path, kind, children_token)
    if lease["writer_lease_protocol"] == "legacy_absent":
        notices.append(
            _notice(
                "writer_lock_legacy_absent",
                "staging directory has no .staging-writer.lock; this is "
                "legacy or the directory-creation-to-lock window. The "
                "writer is unverifiable, not inactive or cleanup-eligible",
                name=path.name,
            )
        )
    reported = {
        "name": path.name,
        "candidate_snapshot_id": candidate,
        "name_valid": name_valid,
        "entry_kind": kind,
        "top_level_entry_count": len(children_reported),
        "top_level_entries": children_reported,
        "has_manifest_json": present["manifest.json"],
        "has_entities_parquet": present["entities.parquet"],
        "has_relationships_parquet": present["relationships.parquet"],
        "has_text_units_parquet": present["text_units.parquet"],
        "has_call_observations_parquet": present["call_observations.parquet"],
        "has_settings_yaml": present["settings.yaml"],
        "complete_payload_candidate": complete,
        "writer_lease_protocol": lease["writer_lease_protocol"],
        "writer_lease_state": lease["writer_lease_state"],
        "writer_lock_present": lease["writer_lock_present"],
        "writer_lock_regular": lease["writer_lock_regular"],
        "ownership_status": "unknown",
        "cleanup_eligible": False,
        "notices": notices,
    }
    token = {
        "name": path.name,
        "entry_kind": kind,
        "dev": info.st_dev,
        "ino": info.st_ino,
        "mode": info.st_mode,
        "mtime_ns": info.st_mtime_ns,
        "size": info.st_size,
        "children": children_token,
        "writer_lease": {
            "protocol": lease["writer_lease_protocol"],
            "state": lease["writer_lease_state"],
            "present": lease["writer_lock_present"],
            "regular": lease["writer_lock_regular"],
            "identity": lease["identity"],
        },
    }
    return reported, token


def _classify_writer_lease(
    path: Path, kind: str, children: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Observe writer-lock protocol metadata without inferring ownership."""
    if kind != "directory":
        return {
            "writer_lease_protocol": "unsafe",
            "writer_lease_state": "unverifiable",
            "writer_lock_present": False,
            "writer_lock_regular": False,
            "identity": None,
        }
    listed = next(
        (child for child in children if child.get("name") == STAGING_WRITER_LOCK_NAME),
        None,
    )
    if listed is None:
        try:
            observation = probe_staging_writer_lease(path)
        except StagingWriterLockUnsafe as error:
            raise SnapshotStagingIntegrityError(str(error)) from error
        except StagingWriterLeaseError as error:
            raise SnapshotStagingError(str(error)) from error
        if observation["writer_lock_present"]:
            raise SnapshotStagingIntegrityError(
                f"staging writer lock appeared while the directory was listed: "
                f"{path / STAGING_WRITER_LOCK_NAME}"
            )
        return {
            "writer_lease_protocol": "legacy_absent",
            "writer_lease_state": "unverifiable",
            "writer_lock_present": False,
            "writer_lock_regular": False,
            "identity": None,
        }
    try:
        observation = probe_staging_writer_lease(path)
    except StagingWriterLockUnsafe as error:
        raise SnapshotStagingIntegrityError(str(error)) from error
    except StagingWriterLeaseError as error:
        raise SnapshotStagingError(str(error)) from error
    if not observation["writer_lock_present"]:
        raise SnapshotStagingIntegrityError(
            f"staging writer lock disappeared while it was inspected: "
            f"{path / STAGING_WRITER_LOCK_NAME}"
        )
    identity = observation.get("identity")
    if identity is None or identity[0] != listed.get("dev") or identity[1] != listed.get("ino"):
        raise SnapshotStagingIntegrityError(
            f"staging writer lock changed while it was inspected: "
            f"{path / STAGING_WRITER_LOCK_NAME}"
        )
    return {
        "writer_lease_protocol": observation["writer_lease_protocol"],
        "writer_lease_state": observation["writer_lease_state"],
        "writer_lock_present": observation["writer_lock_present"],
        "writer_lock_regular": observation["writer_lock_regular"],
        "identity": identity,
    }


def _scan_inventory_state(root: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    _require_managed_graph(root)
    lock_identity = _lock_identity(root)
    current_id, current_identity = _read_current(root)
    published, staging_paths = _list_snapshot_entries(root)
    if current_id not in published:
        raise SnapshotStagingIntegrityError(
            f"current snapshot is missing or dangling: {current_id!r}"
        )
    if len(staging_paths) > MAX_STAGING_ENTRIES:
        raise SnapshotStagingError(
            f"staging entry count {len(staging_paths)} exceeds bound "
            f"{MAX_STAGING_ENTRIES}"
        )
    entries: List[Dict[str, Any]] = []
    staging_tokens: List[Dict[str, Any]] = []
    for path in staging_paths:
        reported, token = _inspect_staging_entry(path)
        entries.append(reported)
        staging_tokens.append(token)
    result: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "graph": str(root),
        "current": current_id,
        "published_snapshots": list(published),
        "published_count": len(published),
        "staging_count": len(entries),
        "staging_entries": entries,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
        "ownership_inference": False,
        "cleanup_supported": False,
    }
    consistency = {
        "current": current_id,
        "current_identity": current_identity,
        "lock_identity": lock_identity,
        "published": list(published),
        "staging": staging_tokens,
    }
    return consistency, result


def _describe_scan_delta(
    first: Mapping[str, Any], second: Mapping[str, Any]
) -> str:
    changed: List[str] = []
    if first.get("current") != second.get("current") or first.get(
        "current_identity"
    ) != second.get("current_identity"):
        changed.append("current")
    if first.get("published") != second.get("published"):
        changed.append("published_snapshots")
    if first.get("lock_identity") != second.get("lock_identity"):
        changed.append("publication_lock")
    first_names = [item.get("name") for item in (first.get("staging") or [])]
    second_names = [item.get("name") for item in (second.get("staging") or [])]
    if first_names != second_names:
        changed.append("staging_names")
    elif first.get("staging") != second.get("staging"):
        first_by_name = {
            item.get("name"): item for item in (first.get("staging") or [])
        }
        for item in second.get("staging") or []:
            prior = first_by_name.get(item.get("name"))
            if prior is None:
                continue
            if prior.get("entry_kind") != item.get("entry_kind"):
                changed.append("staging_type")
            elif (prior.get("dev"), prior.get("ino")) != (
                item.get("dev"),
                item.get("ino"),
            ):
                changed.append("staging_identity")
            elif (prior.get("writer_lease") or {}).get("state") != (
                item.get("writer_lease") or {}
            ).get("state"):
                changed.append("writer_lease_state")
            elif (prior.get("writer_lease") or {}).get("present") != (
                item.get("writer_lease") or {}
            ).get("present") or (prior.get("writer_lease") or {}).get(
                "protocol"
            ) != (item.get("writer_lease") or {}).get("protocol"):
                changed.append("writer_lock_present")
            elif (prior.get("writer_lease") or {}).get("regular") != (
                item.get("writer_lease") or {}
            ).get("regular"):
                changed.append("writer_lock_type")
            elif (prior.get("writer_lease") or {}).get("identity") != (
                item.get("writer_lease") or {}
            ).get("identity"):
                changed.append("writer_lock_identity")
            elif prior.get("children") != item.get("children") or prior.get(
                "mtime_ns"
            ) != item.get("mtime_ns") or prior.get("size") != item.get("size"):
                changed.append("staging_content_metadata")
            elif prior != item:
                changed.append("staging_metadata")
    if not changed:
        changed.append("staging_inventory")
    # Preserve first-seen order without duplicates.
    unique = list(dict.fromkeys(changed))
    return ", ".join(unique)


def build_stable_staging_inventory_unlocked(
    root: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Two-scan inventory. Caller must already hold the shared graph lease.

    Returns ``(consistency, result)``. ``consistency`` is the internal
    stable scan token; do not expose it. Hash it with
    ``staging_state_revision_of`` when a later reader needs to detect
    inode replacement that public inventory fields omit.
    """
    first_token, _first_result = _scan_inventory_state(root)
    second_token, result = _scan_inventory_state(root)
    if first_token != second_token:
        raise SnapshotStagingIntegrityError(
            "staging inventory changed during the read: "
            + _describe_scan_delta(first_token, second_token)
        )
    result["staging_revision"] = staging_revision_of(result)
    return second_token, result


def _build_inventory_unlocked(root: Path) -> Dict[str, Any]:
    _token, result = build_stable_staging_inventory_unlocked(root)
    return result


@contextmanager
def _snapshot_staging_scope(graph: Path) -> Iterator[Dict[str, Any]]:
    """Yield one inventory while its shared existing-lock lease remains held."""
    root = _resolve_graph_root(graph)
    try:
        with graph_read_lease(root, allow_unlocked_managed=False):
            yield _build_inventory_unlocked(root)
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_staging(graph: Path) -> Dict[str, Any]:
    """Build one staging inventory without writing files or process streams."""
    with _snapshot_staging_scope(graph) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report a read-only structural inventory of snapshots/.staging-* "
            "entries, including cooperative writer-lease observations. "
            "Does not delete, quarantine, or infer ownership. Never creates "
            ".publish.lock or .snapshot-pins.json, and is not an MCP tool. "
            "Two-scan agreement is bounded change detection, not a liveness "
            "lease over a staging writer."
        )
    )
    parser.add_argument(
        "--graph",
        "-g",
        type=Path,
        required=True,
        help="Managed BYOG graph root, relative to cwd.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_staging_scope(args.graph) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing the graph lease so the
            # complete response is handed to the caller under that lease.
            sys.stdout.flush()
    except SnapshotStagingError as error:
        print(f"snapshot-staging: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-staging: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
