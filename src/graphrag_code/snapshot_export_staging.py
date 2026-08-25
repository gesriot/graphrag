#!/usr/bin/env python
"""Read-only inventory of private snapshot-export-apply staging names.

``snapshot-export-apply`` publishes through private sibling directories
named ``.graphrag-export-<32 lowercase hex characters>``. A crash before
publication may leave one behind. This command reports a bounded
structural inventory of such direct children under one selected parent
directory. It does not delete, rename, repair, quarantine, plan
cleanup, infer liveness or ownership, or mutate anything.

A syntactically matching name proves only that the name matches the
current protocol. It does not prove that this project created the
entry, that an apply crashed, that no writer is active, or that
deletion is safe. Ownership is always unknown. Writer activity is
always unknown. Cleanup eligibility is always false. Contents are not
inspected. This is not export verification and not a backup,
authenticity, provenance, or recoverability claim.

The command does not inspect a managed graph, read ``current``,
``snapshots/``, pins, graph staging, or ``.publish.lock``, or acquire a
graph lease. For recognized real staging directories only, it may
open the directory descriptor-relative and inspect or nonblocking-probe
the fixed ``.export-writer.lock`` protocol entry. It does not create,
write, truncate, chmod, replace, unlink, or rename that file, and it
does not open or read export payload contents. Observed lease
contention does not change ``writer_activity``. MCP stays exactly 12
read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-export-staging --parent <directory> [--json]
    python -m graphrag_code.snapshot_export_staging --parent <directory> [--json]
    uv run python scripts/snapshot_export_staging.py --parent <directory> [--json]
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from graphrag_code.snapshot_export_writer_lease import (
    ExportWriterLeaseError,
    ExportWriterLeaseIntegrityError,
    close_held_probe_fds,
    probe_export_writer_lease,
    require_export_writer_lock_probe_primitives,
)

STAGING_SCHEMA_VERSION = 1
STAGING_NAME_PREFIX = ".graphrag-export-"
STAGING_NAME_RE = re.compile(r"^\.graphrag-export-[0-9a-f]{32}$")
MAX_PARENT_ENTRIES = 4096
MAX_PREFIXED_ENTRIES = 64
_REVISION_KEYS = (
    "schema_version",
    "staging_entries",
    "staging_count",
    "unsafe_staging_count",
    "unrecognized_prefixed_entries",
    "unrecognized_prefixed_count",
    "other_entry_count",
)
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "name_match_is_not_creation_proof",
        "kind": "notice",
        "message": (
            "A matching .graphrag-export-<32 lowercase hex> name proves "
            "only that the name matches the current protocol. It does "
            "not prove that snapshot-export-apply created the entry."
        ),
    },
    {
        "code": "stable_observation_is_not_writer_absence",
        "kind": "notice",
        "message": (
            "A stable inventory does not prove a writer is absent or "
            "dead. Ownership is unknown. Writer activity is unknown. "
            "No PID, process list, host, wall-clock age, birthtime, "
            "mtime-age, timeout, or open-file heuristic is used."
        ),
    },
    {
        "code": "absence_does_not_prove_cleanup",
        "kind": "notice",
        "message": (
            "An empty inventory does not prove cleanup occurred or that "
            "no leftover exists outside this observation window."
        ),
    },
    {
        "code": "inventory_performs_no_cleanup",
        "kind": "notice",
        "message": (
            "cleanup_supported and cleanup_performed are always false. "
            "This command does not recommend deletion and is not "
            "authorization to delete anything."
        ),
    },
    {
        "code": "contents_not_inspected",
        "kind": "notice",
        "message": (
            "contents_inspected is always false. This inventory does "
            "not open or read export payload contents and is not export "
            "verification. On recognized real directories it may inspect "
            "only the fixed .export-writer.lock protocol entry."
        ),
    },
    {
        "code": "writer_lease_is_not_activity",
        "kind": "notice",
        "message": (
            "writer_lease_state is a cooperative-lease observation only. "
            "held_at_scan does not change writer_activity. A persistent "
            ".export-writer.lock file does not prove a writer exists or "
            "existed. A successful nonblocking probe proves only that "
            "the cooperative lease was not held at that instant. "
            "Contention proves only that a cooperating process held the "
            "lease at that instant. Missing lock metadata is "
            "metadata_absent and unverifiable, not cleanup-eligible. "
            "For metadata_absent, writer_lease_dev, writer_lease_ino, "
            "writer_lease_mode, writer_lease_size, writer_lease_mtime_ns, "
            "and writer_lease_ctime_ns are null. writer_lease_path is "
            "the expected protocol pathname even when the file is absent."
        ),
    },
    {
        "code": "not_backup_or_authenticity",
        "kind": "notice",
        "message": (
            "This report is not a backup, recovery, authenticity, "
            "provenance, or recoverability claim."
        ),
    },
    {
        "code": "observation_window_only",
        "kind": "notice",
        "message": (
            "Changes after the final observation are outside the "
            "observation window."
        ),
    },
    {
        "code": "cli_only_not_mcp",
        "kind": "notice",
        "message": (
            "snapshot-export-staging is CLI-only and intentionally "
            "absent from the fixed 12-tool MCP set."
        ),
    },
)


class HeldExportStagingObservation:
    """Internal two-scan observation. Not part of public JSON."""

    def __init__(
        self,
        *,
        parent_path: Path,
        parent_fd: int,
        parent_identity: Tuple[int, int, int, int, int, int],
        inventory: Dict[str, Any],
        held: Dict[str, Tuple[Optional[int], Optional[int]]],
    ) -> None:
        self.parent_path = parent_path
        self.parent_fd = parent_fd
        self.parent_identity = parent_identity
        self.inventory = inventory
        self.held = held


class SnapshotExportStagingError(Exception):
    """Malformed arguments, missing parent, bounds, or unsupported platform. Exit 2."""

    exit_code = 2


class SnapshotExportStagingIntegrityError(SnapshotExportStagingError):
    """Concurrent listing, identity, metadata, or pathname change. Exit 1."""

    exit_code = 1


def _after_parent_path_inspected(path: Path) -> None:
    return None


def _after_parent_opened(path: Path, parent_fd: int) -> None:
    return None


def _after_first_scan(path: Path, scan: Mapping[str, Any]) -> None:
    return None


def _after_result_ready(path: Path, parent_fd: int, result: Mapping[str, Any]) -> None:
    return None


def _after_probe_descriptors_ready(
    path: Path,
    parent_fd: int,
    held: Mapping[str, Tuple[Optional[int], Optional[int]]],
) -> None:
    return None


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
    return (
        "snapshot-export-staging: "
        f"parent={result.get('parent')} "
        f"staging={result.get('staging_count')} "
        f"unrecognized={result.get('unrecognized_prefixed_count')} "
        f"other={result.get('other_entry_count')} "
        f"inventory_revision={result.get('inventory_revision')}"
        f"{suffix} "
        "inventory-only; no cleanup; not authorization to delete"
    )


def canonical_inventory_revision_payload(result: Mapping[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    for key in _REVISION_KEYS:
        if key not in result:
            raise SnapshotExportStagingError(
                f"export staging inventory is missing revision input {key!r}"
            )
        payload[key] = result[key]
    return payload


def canonical_inventory_revision_text(result: Mapping[str, Any]) -> str:
    return json.dumps(
        canonical_inventory_revision_payload(result),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def inventory_revision_of(result: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        canonical_inventory_revision_text(result).encode("utf-8")
    ).hexdigest()
    return "sha256:" + digest


def is_current_export_staging_name(name: str) -> bool:
    return bool(STAGING_NAME_RE.fullmatch(name))


def is_export_staging_prefix_name(name: str) -> bool:
    return name.startswith(STAGING_NAME_PREFIX)


def _name_sort_key(name: str) -> bytes:
    """Order filesystem names by their underlying POSIX byte representation."""
    return os.fsencode(name)


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


def _parent_identity(info: os.stat_result) -> Tuple[int, int, int, int, int, int]:
    """Complete parent token. Includes ctime so rename-away-and-back is visible."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _child_identity(
    info: os.stat_result,
) -> Tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _require_descriptor_reads() -> None:
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_dir_fd", set())
        or os.scandir not in getattr(os, "supports_fd", set())
    ):
        raise SnapshotExportStagingError(
            "safe descriptor-relative no-follow parent inventory is unsupported on "
            f"this platform: {sys.platform!r}"
        )
    try:
        require_export_writer_lock_probe_primitives()
    except ExportWriterLeaseError as error:
        raise SnapshotExportStagingError(str(error)) from error


def _wrap_writer_lease_error(error: ExportWriterLeaseError) -> SnapshotExportStagingError:
    if isinstance(error, ExportWriterLeaseIntegrityError):
        return SnapshotExportStagingIntegrityError(str(error))
    return SnapshotExportStagingError(str(error))


def _parent_path(parent: object) -> Path:
    if parent is None or (isinstance(parent, str) and parent == ""):
        raise SnapshotExportStagingError("parent is required")
    path = Path(parent)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path


def _open_parent(path: Path) -> Tuple[int, Tuple[int, int, int, int, int, int]]:
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotExportStagingError(f"parent does not exist: {path}") from error
    except OSError as error:
        raise SnapshotExportStagingError(
            f"cannot inspect parent {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotExportStagingError(
            f"parent must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotExportStagingError(f"parent is not a real directory: {path}")
    _after_parent_path_inspected(path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(path), flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise SnapshotExportStagingIntegrityError(
                f"parent changed or became unsafe while opening it: {path}"
            ) from error
        raise SnapshotExportStagingError(
            f"cannot safely open parent {path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = path.lstat()
        except OSError as error:
            raise SnapshotExportStagingIntegrityError(
                f"parent changed while opening it: {path}"
            ) from error
        opened_identity = _parent_identity(opened)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or opened_identity != _parent_identity(before)
            or _parent_identity(current) != opened_identity
        ):
            raise SnapshotExportStagingIntegrityError(
                f"parent changed or became unsafe while opening it: {path}"
            )
    except Exception:
        os.close(fd)
        raise
    return fd, opened_identity


def _canonical_parent(
    path: Path,
    parent_fd: int,
    expected_identity: Tuple[int, int, int, int, int, int],
) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SnapshotExportStagingIntegrityError(
            f"parent changed during canonicalization: {path}"
        ) from error
    try:
        resolved_info = resolved.lstat()
        held = os.fstat(parent_fd)
    except OSError as error:
        raise SnapshotExportStagingIntegrityError(
            f"parent changed during canonicalization: {path}"
        ) from error
    if (
        stat.S_ISLNK(resolved_info.st_mode)
        or not stat.S_ISDIR(resolved_info.st_mode)
        or not stat.S_ISDIR(held.st_mode)
        or _parent_identity(held) != expected_identity
        or _parent_identity(resolved_info) != expected_identity
    ):
        raise SnapshotExportStagingIntegrityError(
            f"parent changed or no longer names the held directory: {path}"
        )
    return resolved


def _require_parent_held(
    path: Path,
    parent_fd: int,
    expected_identity: Tuple[int, int, int, int, int, int],
) -> Tuple[int, int, int, int, int, int]:
    try:
        held = os.fstat(parent_fd)
        current = path.lstat()
    except OSError as error:
        raise SnapshotExportStagingIntegrityError(
            f"parent changed during inventory: {path}"
        ) from error
    held_identity = _parent_identity(held)
    if (
        not stat.S_ISDIR(held.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or held_identity != expected_identity
        or _parent_identity(current) != expected_identity
    ):
        raise SnapshotExportStagingIntegrityError(
            f"parent changed or no longer names the held directory: {path}"
        )
    return held_identity


def _child_record(
    parent_resolved: Path, name: str, info: os.stat_result, *, matches: bool
) -> Dict[str, Any]:
    dev, ino, size, mtime_ns, ctime_ns, mode = _child_identity(info)
    return {
        "name": name,
        "path": str(parent_resolved / name),
        "kind": _entry_kind(info.st_mode),
        "name_matches_current_protocol": matches,
        "ownership": "unknown",
        "writer_activity": "unknown",
        "cleanup_eligible": False,
        "contents_inspected": False,
        "dev": dev,
        "ino": ino,
        "mode": mode,
        "size": size,
        "mtime_ns": mtime_ns,
        "ctime_ns": ctime_ns,
    }


def _list_parent_children(
    parent_fd: int, parent_path: Path
) -> List[Tuple[str, os.stat_result]]:
    entries: List[Tuple[str, os.stat_result]] = []
    try:
        with os.scandir(parent_fd) as iterator:
            for entry in iterator:
                if len(entries) >= MAX_PARENT_ENTRIES:
                    raise SnapshotExportStagingError(
                        f"parent entry count exceeds bound {MAX_PARENT_ENTRIES}: "
                        f"{parent_path}"
                    )
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise SnapshotExportStagingIntegrityError(
                        f"cannot inspect parent child {parent_path / entry.name}: "
                        f"{error}"
                    ) from error
                entries.append((entry.name, info))
    except SnapshotExportStagingError:
        raise
    except OSError as error:
        raise SnapshotExportStagingIntegrityError(
            f"cannot list parent {parent_path}: {error}"
        ) from error
    entries.sort(key=lambda item: _name_sort_key(item[0]))
    return entries


def _scan_parent(
    parent_resolved: Path,
    parent_fd: int,
    expected_identity: Tuple[int, int, int, int, int, int],
    *,
    keep_descriptors: bool = False,
) -> Tuple[Dict[str, Any], Dict[str, Tuple[Optional[int], Optional[int]]]]:
    _require_parent_held(parent_resolved, parent_fd, expected_identity)
    children = _list_parent_children(parent_fd, parent_resolved)
    _require_parent_held(parent_resolved, parent_fd, expected_identity)
    staging: List[Dict[str, Any]] = []
    unrecognized: List[Dict[str, Any]] = []
    other_count = 0
    held: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    try:
        for name, info in children:
            if is_current_export_staging_name(name):
                record = _child_record(parent_resolved, name, info, matches=True)
                if record["kind"] == "directory":
                    try:
                        observation, staging_fd, lock_fd = probe_export_writer_lease(
                            parent_fd=parent_fd,
                            staging_name=name,
                            staging_info=info,
                            staging_path=parent_resolved / name,
                            keep_descriptors=keep_descriptors,
                        )
                    except ExportWriterLeaseError as error:
                        raise _wrap_writer_lease_error(error) from error
                    record.update(observation)
                    if keep_descriptors:
                        held[name] = (staging_fd, lock_fd)
                staging.append(record)
            elif is_export_staging_prefix_name(name):
                unrecognized.append(
                    _child_record(parent_resolved, name, info, matches=False)
                )
            else:
                other_count += 1
            if len(staging) + len(unrecognized) > MAX_PREFIXED_ENTRIES:
                raise SnapshotExportStagingError(
                    f"prefixed export-staging entry count exceeds bound "
                    f"{MAX_PREFIXED_ENTRIES}: {parent_resolved}"
                )
        _require_parent_held(parent_resolved, parent_fd, expected_identity)
        return (
            {
                "parent_identity": expected_identity,
                "staging_entries": staging,
                "unrecognized_prefixed_entries": unrecognized,
                "other_entry_count": other_count,
            },
            held,
        )
    except Exception:
        close_held_probe_fds(held)
        raise


def _describe_scan_delta(first: Mapping[str, Any], second: Mapping[str, Any]) -> str:
    if first.get("parent_identity") != second.get("parent_identity"):
        return "parent identity"
    first_names = [item.get("name") for item in (first.get("staging_entries") or [])]
    second_names = [item.get("name") for item in (second.get("staging_entries") or [])]
    first_unrec = [
        item.get("name") for item in (first.get("unrecognized_prefixed_entries") or [])
    ]
    second_unrec = [
        item.get("name") for item in (second.get("unrecognized_prefixed_entries") or [])
    ]
    if first_names != second_names or first_unrec != second_unrec:
        return "listing"
    if first.get("other_entry_count") != second.get("other_entry_count"):
        return "listing"
    first_by_name = {
        item.get("name"): item
        for item in list(first.get("staging_entries") or [])
        + list(first.get("unrecognized_prefixed_entries") or [])
    }
    for item in list(second.get("staging_entries") or []) + list(
        second.get("unrecognized_prefixed_entries") or []
    ):
        prior = first_by_name.get(item.get("name"))
        if prior is None:
            continue
        if (prior.get("dev"), prior.get("ino")) != (item.get("dev"), item.get("ino")):
            return "entry identity"
        if prior.get("kind") != item.get("kind") or prior.get("mode") != item.get(
            "mode"
        ):
            return "entry metadata"
        if (
            prior.get("size") != item.get("size")
            or prior.get("mtime_ns") != item.get("mtime_ns")
            or prior.get("ctime_ns") != item.get("ctime_ns")
        ):
            return "entry metadata"
        if prior.get("writer_lease_state") != item.get("writer_lease_state"):
            return "writer-lease state"
        if any(
            prior.get(key) != item.get(key)
            for key in (
                "writer_lease_metadata_present",
                "writer_lease_contended",
                "writer_lease_path",
                "writer_lease_dev",
                "writer_lease_ino",
                "writer_lease_mode",
                "writer_lease_size",
                "writer_lease_mtime_ns",
                "writer_lease_ctime_ns",
            )
        ):
            return "writer-lease identity"
        if prior != item:
            return "entry metadata"
    return "inventory"


def _build_result(
    parent_resolved: Path, scan: Mapping[str, Any]
) -> Dict[str, Any]:
    staging = list(scan["staging_entries"])
    unrecognized = list(scan["unrecognized_prefixed_entries"])
    unsafe = sum(1 for entry in staging if entry.get("kind") != "directory")
    result: Dict[str, Any] = {
        "schema_version": STAGING_SCHEMA_VERSION,
        "ok": True,
        "parent": str(parent_resolved),
        "staging_entries": staging,
        "staging_count": len(staging),
        "unsafe_staging_count": unsafe,
        "unrecognized_prefixed_entries": unrecognized,
        "unrecognized_prefixed_count": len(unrecognized),
        "other_entry_count": scan["other_entry_count"],
        "ownership_known": False,
        "writer_activity_known": False,
        "cleanup_supported": False,
        "cleanup_performed": False,
        "parent_mutated": False,
        "graph_inspected": False,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }
    result["inventory_revision"] = inventory_revision_of(result)
    return result


@contextmanager
def held_export_staging_observation_scope(
    parent: object,
) -> Iterator[HeldExportStagingObservation]:
    """Yield one two-scan observation while parent and probe descriptors stay held.

    Internal helper for inventory, cleanup plan, cleanup apply, and
    cleanup reconcile. The yielded object is not public JSON and must
    not be serialized.
    """
    _require_descriptor_reads()
    path = _parent_path(parent)
    parent_fd, parent_identity = _open_parent(path)
    held: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    try:
        parent_resolved = _canonical_parent(path, parent_fd, parent_identity)
        _after_parent_opened(parent_resolved, parent_fd)
        first, first_held = _scan_parent(
            parent_resolved, parent_fd, parent_identity, keep_descriptors=False
        )
        close_held_probe_fds(first_held)
        _after_first_scan(parent_resolved, first)
        second, held = _scan_parent(
            parent_resolved, parent_fd, parent_identity, keep_descriptors=True
        )
        if first != second:
            raise SnapshotExportStagingIntegrityError(
                "export staging inventory changed during the read: "
                + _describe_scan_delta(first, second)
            )
        _require_parent_held(parent_resolved, parent_fd, parent_identity)
        result = _build_result(parent_resolved, second)
        _after_result_ready(parent_resolved, parent_fd, result)
        _after_probe_descriptors_ready(parent_resolved, parent_fd, held)
        yield HeldExportStagingObservation(
            parent_path=parent_resolved,
            parent_fd=parent_fd,
            parent_identity=parent_identity,
            inventory=result,
            held=held,
        )
    except ExportWriterLeaseError as error:
        raise _wrap_writer_lease_error(error) from error
    finally:
        close_held_probe_fds(held)
        os.close(parent_fd)


@contextmanager
def _snapshot_export_staging_scope(parent: object) -> Iterator[Dict[str, Any]]:
    """Yield one inventory while the parent descriptor stays held."""
    with held_export_staging_observation_scope(parent) as observation:
        yield observation.inventory


@contextmanager
def export_staging_observation_scope(parent: object) -> Iterator[Dict[str, Any]]:
    """Yield one two-scan inventory while parent and probe descriptors stay held.

    Shared by snapshot-export-staging, the read-only cleanup plan, and
    snapshot-export-staging-cleanup-reconcile. This is not a public CLI,
    not a second path-based scan, and does not inspect a managed graph.
    """
    with _snapshot_export_staging_scope(parent) as result:
        yield result


def snapshot_export_staging(parent: Path) -> Dict[str, Any]:
    """Build one export-staging inventory without writing files."""
    with export_staging_observation_scope(parent) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report a read-only structural inventory of direct "
            ".graphrag-export-* children under one parent directory. "
            "Does not delete, infer ownership, or inspect export payload "
            "contents. May observe .export-writer.lock on recognized "
            "real directories only. cleanup_supported stays false. "
            "snapshot-export-staging-cleanup-plan is the separate "
            "read-only schema-2 classification command. "
            "snapshot-export-staging-cleanup is the separate CAS apply. "
            "Not an MCP tool."
        )
    )
    parser.add_argument(
        "--parent",
        type=Path,
        required=True,
        help="Parent directory to inventory, relative to cwd.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_export_staging_scope(args.parent) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            sys.stdout.flush()
            return 0 if result["ok"] else 1
    except SnapshotExportStagingError as error:
        print(f"snapshot-export-staging: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-export-staging: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
