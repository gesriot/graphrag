#!/usr/bin/env python
"""CAS-guarded snapshot export apply.

``snapshot-export-apply`` copies exactly one retained published
snapshot's accepted direct envelope payload files into a newly created
standalone destination directory. It does not mutate the managed graph,
does not create or rewrite ``.publish.lock``, does not change
``current``, snapshots, pins, staging, manifests, or payloads, and does
not overwrite, merge with, delete, or rename a pre-existing destination
entry. It does not create an archive. It does not preserve or claim to
preserve ownership, timestamps, xattrs, ACLs, hardlinks, or provenance.
The copy is not a backup, authentic, recoverable, or authorization to
delete anything. MCP stays exactly 11 read-only tools; this command is
CLI-only.

``--snapshot``, ``--destination``, ``--expected-export-revision``, and
``--export-confirmed`` are required. The expected revision is a
compare-and-swap token only. A saved plan file is not accepted. One
shared existing-lock graph lease covers fresh plan recomputation, CAS,
source reopening, copying, destination verification, atomic
publication, result construction, stdout write, and flush. The command
does not call a public scope that takes a nested graph lease.

Publication uses a private sibling staging directory created
descriptor-relative under the destination parent. Immediately after
that directory is anchored, apply creates ``.export-writer.lock``
and holds an exclusive advisory writer lease for the complete
mutable staging-write interval. That owned lock pathname is removed
while the lease is still held, then the lease is released before
atomic publication. The
published destination never contains ``.export-writer.lock``. A
crash may leave the private staging directory and, if the lock was
created, the regular lock file. Process death releases the kernel
lease but does not remove the file. This command does not add a
staging cleanup tool. After atomic publication succeeds, a later
reporting failure never deletes the destination.

Usage:
    graphrag-code snapshot-export-apply --graph <root> --snapshot <id|current> \\
        --destination <new-dir> --expected-export-revision sha256:<hex> \\
        --export-confirmed [--json]
    python -m graphrag_code.snapshot_export_apply --graph <root> --snapshot <id|current> \\
        --destination <new-dir> --expected-export-revision sha256:<hex> \\
        --export-confirmed [--json]
    uv run python scripts/snapshot_export_apply.py --graph <root> --snapshot <id|current> \\
        --destination <new-dir> --expected-export-revision sha256:<hex> \\
        --export-confirmed [--json]
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from graphrag_code._rename_noreplace import (
    RenameNoreplaceError,
    rename_directory_noreplace,
    rename_noreplace_supported,
)
from graphrag_code.byog_graph import (
    ByogPublicationLockError,
    ByogReaderLockError,
    graph_read_lease,
)
from graphrag_code.snapshot_export_plan import (
    ACCEPTED_PAYLOAD_FILES,
    HASH_CHUNK_BYTES,
    SnapshotExportPlanError,
    SnapshotExportPlanIntegrityError,
    _capture_tokens,
    _directory_identity,
    _file_identity,
    _held_snapshot_export_plan_unlocked,
    _is_canonical_direct_name,
    _lock_error,
    _open_regular_nofollow,
    _parse_snapshot,
    _payload_children,
    _require_descriptor_reads,
    _require_managed_graph,
    _resolve_graph_root,
    _stream_regular_file,
    _wrap_staging_error,
)
from graphrag_code.snapshot_export_writer_lease import (
    EXPORT_STAGING_WRITER_LOCK_NAME,
    ExportWriterLeaseError,
    ExportWriterLeaseIntegrityError,
    HeldExportWriterLease,
    acquire_export_writer_lease,
    cleanup_owned_export_writer_lock,
    prove_export_writer_lock_absent,
    require_export_writer_lock_primitives,
)
from graphrag_code.snapshot_read import CURRENT_REF
from graphrag_code.snapshot_staging import SnapshotStagingError

APPLY_SCHEMA_VERSION = 1
STAGING_NAME_PREFIX = ".graphrag-export-"
STAGING_DIR_MODE = 0o700
PAYLOAD_FILE_MODE = 0o600
STAGING_CREATE_ATTEMPTS = 8
_MAX_ERROR_CHARS = 400
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_CONFIRMATION_MESSAGE = """\
refusing to apply snapshot export without --export-confirmed.

This is an explicit CLI copy of one retained published snapshot's
direct envelope payload files into a newly created destination
directory. snapshot-export-plan is the preview; this command has no
dry-run. Confirmation is required even when the planned file set is
nonempty. The copy is not a backup, archive, restore kit, or
authorization to delete anything. It does not preserve ownership,
timestamps, xattrs, ACLs, hardlinks, or provenance.

--expected-export-revision is a compare-and-swap guard: the shared
existing-lock lease recomputes a fresh export plan and copies only
when that token still matches. A mismatched revision creates nothing.
A crash may leave the private sibling staging directory and its
.export-writer.lock protocol file. This command does not add a
staging cleanup tool.
""".strip()
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "export_is_not_backup",
        "kind": "notice",
        "message": (
            "The destination is a newly created copy of the accepted "
            "direct envelope payload files. It is not a backup, archive, "
            "restore kit, authentic replica, or recoverable image, and "
            "it is not authorization to delete anything."
        ),
    },
    {
        "code": "export_revision_is_cas_only",
        "kind": "notice",
        "message": (
            "expected_export_revision is a compare-and-swap token for "
            "the freshly recomputed export plan. It does not prove "
            "provenance, authenticity, or recoverability."
        ),
    },
    {
        "code": "metadata_not_preserved",
        "kind": "notice",
        "message": (
            "The copy does not preserve or claim to preserve ownership, "
            "timestamps, xattrs, ACLs, hardlinks, or provenance."
        ),
    },
    {
        "code": "crash_may_leave_private_staging",
        "kind": "notice",
        "message": (
            "A crash before atomic publication may leave the private "
            "sibling staging directory and, if created, the regular "
            ".export-writer.lock protocol file. Process death releases "
            "the kernel lease but does not remove that file. This "
            "command does not add a staging cleanup tool. After "
            "publication succeeds the destination is never automatically "
            "deleted."
        ),
    },
    {
        "code": "export_writer_lease_not_ownership",
        "kind": "notice",
        "message": (
            "The private .export-writer.lock file is protocol metadata, "
            "not proof of ownership, writer identity, writer death, "
            "crash, or cleanup eligibility. The pathname is removed "
            "while the advisory lease is still held, then the lease is "
            "released before atomic publication; it is not externally "
            "observable after that "
            "removal. The published destination never contains "
            ".export-writer.lock."
        ),
    },
    {
        "code": "advisory_locks_cooperating_only",
        "kind": "notice",
        "message": (
            "Advisory locks protect only cooperating processes. "
            "This is not continuous protection against lock-ignoring "
            "actors, and changes after the final source observation "
            "are not covered."
        ),
    },
    {
        "code": "cli_only_not_mcp",
        "kind": "notice",
        "message": (
            "snapshot-export-apply is CLI-only and intentionally absent "
            "from the fixed 11-tool MCP set."
        ),
    },
)


class SnapshotExportApplyError(Exception):
    """Expected export-apply refusal. Default exit 2."""

    exit_code = 2


class SnapshotExportApplyIntegrityError(SnapshotExportApplyError):
    """Unsafe structure or lock-ignoring change during apply. Exit 1."""

    exit_code = 1


def parse_export_revision(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotExportApplyError(
            "expected-export-revision must be sha256:<64 lowercase hex>"
        )
    if value != value.strip():
        raise SnapshotExportApplyError(
            "expected-export-revision must not contain leading or trailing "
            f"whitespace: {value!r}"
        )
    if not _REVISION_RE.fullmatch(value):
        raise SnapshotExportApplyError(
            "expected-export-revision must be sha256:<64 lowercase hex>, "
            f"got {value!r}"
        )
    return value


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
    text = (
        "snapshot-export-apply: "
        f"graph={result.get('graph')} "
        f"requested={result.get('requested_snapshot')} "
        f"resolved={result.get('resolved_snapshot')} "
        f"destination={result.get('destination')} "
        f"files={result.get('file_count')} "
        f"total_size_bytes={result.get('total_size_bytes')} "
        f"expected_export_revision={result.get('expected_export_revision')} "
        f"observed_export_revision={result.get('observed_export_revision')} "
        f"ok={str(bool(result.get('ok'))).lower()} "
        f"partial={str(bool(result.get('partial'))).lower()} "
        f"export_performed={str(bool(result.get('export_performed'))).lower()} "
        f"destination_created={str(bool(result.get('destination_created'))).lower()} "
        f"destination_verified={str(bool(result.get('destination_verified'))).lower()} "
        f"source_unchanged={str(bool(result.get('source_unchanged'))).lower()} "
        f"parent_fsync_confirmed={str(bool(result.get('parent_fsync_confirmed'))).lower()}"
    )
    error = result.get("error")
    if isinstance(error, str) and error:
        text += f" error={error}"
    return (
        text
        + " This copy is not a backup and is not authorization to delete anything."
    )


def _wrap_plan_error(error: Exception) -> SnapshotExportApplyError:
    if isinstance(error, SnapshotExportPlanIntegrityError):
        return SnapshotExportApplyIntegrityError(str(error))
    if isinstance(error, SnapshotExportPlanError):
        wrapped = SnapshotExportApplyError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    if isinstance(error, SnapshotStagingError):
        return _wrap_staging_error_as_apply(error)
    return SnapshotExportApplyError(str(error))


def _wrap_staging_error_as_apply(error: Exception) -> SnapshotExportApplyError:
    wrapped = _wrap_staging_error(error)
    if isinstance(wrapped, SnapshotExportPlanIntegrityError):
        return SnapshotExportApplyIntegrityError(str(wrapped))
    out = SnapshotExportApplyError(str(wrapped))
    out.exit_code = getattr(wrapped, "exit_code", 2)
    return out


def _apply_lock_error(error: Exception) -> SnapshotExportApplyError:
    wrapped = _lock_error(error)
    if isinstance(wrapped, SnapshotExportPlanIntegrityError):
        return SnapshotExportApplyIntegrityError(str(wrapped))
    if "publication lock is missing" in str(error):
        return SnapshotExportApplyError(f"{error}\n{_MISSING_LOCK_HINT}")
    out = SnapshotExportApplyError(str(wrapped))
    out.exit_code = getattr(wrapped, "exit_code", 2)
    return out


def _require_export_apply_primitives() -> None:
    _require_descriptor_reads()
    supported = getattr(os, "supports_dir_fd", set())
    if (
        os.mkdir not in supported
        or os.unlink not in supported
        or os.rmdir not in supported
        or os.open not in supported
        or os.stat not in supported
    ):
        raise SnapshotExportApplyError(
            "safe descriptor-relative exclusive export publication is "
            f"unsupported on this platform: {sys.platform!r}"
        )
    if not rename_noreplace_supported():
        raise SnapshotExportApplyError(
            "atomic no-replace directory publication is unsupported on "
            f"this platform: {sys.platform!r}"
        )
    try:
        require_export_writer_lock_primitives()
    except ExportWriterLeaseError as error:
        raise SnapshotExportApplyError(str(error)) from error


def _wrap_writer_lease_error(error: ExportWriterLeaseError) -> SnapshotExportApplyError:
    if isinstance(error, ExportWriterLeaseIntegrityError):
        return SnapshotExportApplyIntegrityError(str(error))
    return SnapshotExportApplyError(str(error))


def _read_chunk(fd: int, size: int) -> bytes:
    return os.read(fd, size)


def _write_chunk(fd: int, data: bytes) -> int:
    return os.write(fd, data)


def _fsync(fd: int) -> None:
    os.fsync(fd)


def _io_error(action: str, error: BaseException) -> SnapshotExportApplyError:
    return SnapshotExportApplyError(f"{action} failed: {error}")


def _bound_error(message: object) -> str:
    text = " ".join(str(message).split())
    if len(text) > _MAX_ERROR_CHARS:
        return text[:_MAX_ERROR_CHARS]
    return text


def _fsync_file(fd: int) -> None:
    try:
        _fsync(fd)
    except OSError as error:
        raise _io_error("fsync export payload", error) from error


def _fsync_directory(fd: int) -> None:
    try:
        _fsync(fd)
    except OSError as error:
        raise _io_error("fsync export directory", error) from error


def _publish_staging(parent_fd: int, staging_name: str, dest_name: str) -> None:
    rename_directory_noreplace(parent_fd, staging_name, parent_fd, dest_name)


def _after_export_apply_plan_computed(
    _root: Path, _plan: Mapping[str, Any]
) -> None:
    """Test hook after the fresh plan and CAS, before destination staging."""
    return None


def _after_export_apply_staging_created(
    _parent_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    """Test hook after private staging exists and before payload copies."""
    return None


def _after_export_apply_writer_lease(
    _parent_fd: int, _staging_name: str, _staging_fd: int, _lock_fd: int
) -> None:
    """Test hook after the exclusive export-writer lease is held."""
    return None


def _before_export_apply_writer_lease_release(
    _parent_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    """Test hook immediately before the owned writer-lock is released."""
    return None


def _after_export_apply_writer_lease_removed(
    _parent_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    """Test hook after writer-lock removal and before envelope re-verify."""
    return None


def _after_export_apply_copied(
    _root: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after payload copies and before staged verification."""
    return None


def _after_export_apply_staged_verified(
    _root: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after staged verification and before source reobservation."""
    return None


def _after_export_apply_source_reobserved(
    _root: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after source reobservation and before publication."""
    return None


def _after_export_apply_staging_mkdir(
    _parent_fd: int, _staging_name: str
) -> None:
    """Test hook after mkdir and first identity capture, before open."""
    return None


def _after_export_apply_staging_opened(
    _parent_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    """Test hook after the staging descriptor is accepted."""
    return None


def _before_export_apply_staging_cleanup(
    _parent_fd: int, _staging_name: str
) -> None:
    """Test hook at the start of best-effort owned staging cleanup."""
    return None


def _before_export_apply_staging_rmdir(
    _parent_fd: int, _staging_name: str
) -> None:
    """Test hook immediately before the proven-empty staging rmdir."""
    return None


def _before_export_apply_publication(
    _parent_fd: int, _dest_name: str, _staging_name: str
) -> None:
    """Test hook immediately before the atomic no-replace publish."""
    return None


def _after_export_apply_published(
    _parent_fd: int, _dest_name: str
) -> None:
    """Test hook after native rename returns and before dest identity proof."""
    return None


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        try:
            written = _write_chunk(fd, data[offset:])
        except OSError as error:
            raise _io_error("write export payload", error) from error
        if written <= 0:
            raise SnapshotExportApplyError("short write while copying export payload")
        offset += written


def _inspect_absent_destination(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise SnapshotExportApplyError(
            f"cannot inspect destination {path}: {error}"
        ) from error
    raise SnapshotExportApplyError(f"destination already exists: {path}")


def _resolve_destination(destination: object) -> Tuple[Path, Path, str]:
    if destination is None or (isinstance(destination, str) and destination == ""):
        raise SnapshotExportApplyError("destination is required")
    path = Path(destination)
    if not path.is_absolute():
        path = Path.cwd() / path
    dest_name = path.name
    if not _is_canonical_direct_name(dest_name):
        raise SnapshotExportApplyError(
            f"destination name is not a canonical direct name: {dest_name!r}"
        )
    parent = path.parent
    try:
        parent_info = parent.lstat()
    except FileNotFoundError as error:
        raise SnapshotExportApplyError(
            f"destination parent does not exist: {parent}"
        ) from error
    except OSError as error:
        raise SnapshotExportApplyError(
            f"cannot inspect destination parent {parent}: {error}"
        ) from error
    if stat.S_ISLNK(parent_info.st_mode):
        raise SnapshotExportApplyError(
            f"destination parent must be a real directory, not a symlink: {parent}"
        )
    if not stat.S_ISDIR(parent_info.st_mode):
        raise SnapshotExportApplyError(
            f"destination parent is not a real directory: {parent}"
        )
    _inspect_absent_destination(path)
    parent_resolved = parent.resolve()
    try:
        resolved_info = parent_resolved.lstat()
    except OSError as error:
        raise SnapshotExportApplyError(
            f"cannot inspect resolved destination parent {parent_resolved}: {error}"
        ) from error
    if stat.S_ISLNK(resolved_info.st_mode) or not stat.S_ISDIR(resolved_info.st_mode):
        raise SnapshotExportApplyError(
            f"destination parent must be a real directory, not a symlink: {parent_resolved}"
        )
    dest_resolved = parent_resolved / dest_name
    _inspect_absent_destination(dest_resolved)
    return parent_resolved, dest_resolved, dest_name


def _open_dest_parent(path: Path) -> Tuple[int, Tuple[int, int, int, int]]:
    _require_export_apply_primitives()
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotExportApplyError(
            f"destination parent does not exist: {path}"
        ) from error
    except OSError as error:
        raise SnapshotExportApplyError(
            f"cannot inspect destination parent {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotExportApplyError(
            f"destination parent must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotExportApplyError(
            f"destination parent is not a real directory: {path}"
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(path), flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise SnapshotExportApplyError(
                f"destination parent changed or became unsafe while opening it: {path}"
            ) from error
        raise SnapshotExportApplyError(
            f"cannot safely open destination parent {path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = path.lstat()
        except OSError as error:
            raise SnapshotExportApplyError(
                f"destination parent changed while opening it: {path}"
            ) from error
        opened_identity = _directory_identity(opened)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or opened_identity != _directory_identity(before)
            or _directory_identity(current) != opened_identity
        ):
            raise SnapshotExportApplyError(
                f"destination parent changed or became unsafe while opening it: {path}"
            )
    except Exception:
        os.close(fd)
        raise
    return fd, opened_identity


def _child_absent(directory_fd: int, name: str, *, label: str) -> None:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise SnapshotExportApplyError(
            f"cannot inspect {label} {name!r}: {error}"
        ) from error
    raise SnapshotExportApplyError(f"{label} already exists: {name}")


def _directory_inode(info: os.stat_result) -> Tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _require_destination_parent_is_held(
    path: Path,
    parent_fd: int,
    opened_identity: Tuple[int, int, int, int],
) -> None:
    """Prove the reported parent still names the anchored directory."""
    expected = (opened_identity[0], opened_identity[1])
    try:
        held = os.fstat(parent_fd)
        current = path.lstat()
    except OSError as error:
        raise SnapshotExportApplyIntegrityError(
            f"destination parent changed while export was in progress: {path}"
        ) from error
    if (
        not stat.S_ISDIR(held.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _directory_inode(held) != expected
        or _directory_inode(current) != expected
    ):
        raise SnapshotExportApplyIntegrityError(
            f"destination parent no longer names the held directory: {path}"
        )


def _path_directory_inode(
    directory_fd: int, name: str, *, label: str
) -> Tuple[int, int]:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise SnapshotExportApplyIntegrityError(
            f"{label} disappeared: {name}"
        ) from error
    except OSError as error:
        raise SnapshotExportApplyError(
            f"cannot inspect {label} {name}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotExportApplyIntegrityError(f"unsafe symlinked {label}: {name}")
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotExportApplyIntegrityError(
            f"{label} is not a directory: {name}"
        )
    return _directory_inode(info)


def _create_private_staging(parent_fd: int) -> Tuple[str, int, Tuple[int, int]]:
    last_error: Optional[OSError] = None
    for _ in range(STAGING_CREATE_ATTEMPTS):
        name = STAGING_NAME_PREFIX + secrets.token_hex(16)
        if not _is_canonical_direct_name(name):
            raise SnapshotExportApplyError(
                f"generated staging name is not a canonical direct name: {name!r}"
            )
        try:
            os.mkdir(name, STAGING_DIR_MODE, dir_fd=parent_fd)
        except FileExistsError as error:
            last_error = error
            continue
        except OSError as error:
            raise SnapshotExportApplyError(
                f"cannot create private export staging directory: {error}"
            ) from error
        try:
            created = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotExportApplyError(
                f"cannot inspect private export staging directory {name}: {error}"
            ) from error
        if stat.S_ISLNK(created.st_mode) or not stat.S_ISDIR(created.st_mode):
            raise SnapshotExportApplyIntegrityError(
                f"private export staging pathname is not a real directory: {name}"
            )
        identity = _directory_inode(created)
        _after_export_apply_staging_mkdir(parent_fd, name)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            staging_fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as error:
            _cleanup_owned_staging(parent_fd, name, identity, {})
            raise SnapshotExportApplyError(
                f"cannot open private export staging directory: {error}"
            ) from error
        try:
            opened = os.fstat(staging_fd)
            path_identity = _path_directory_inode(
                parent_fd, name, label="private export staging directory"
            )
            if (
                not stat.S_ISDIR(opened.st_mode)
                or _directory_inode(opened) != identity
                or path_identity != identity
            ):
                raise SnapshotExportApplyIntegrityError(
                    "private export staging directory changed while opening it"
                )
        except Exception:
            os.close(staging_fd)
            raise
        _after_export_apply_staging_opened(parent_fd, name, staging_fd)
        try:
            opened = os.fstat(staging_fd)
            path_identity = _path_directory_inode(
                parent_fd, name, label="private export staging directory"
            )
            if _directory_inode(opened) != identity or path_identity != identity:
                raise SnapshotExportApplyIntegrityError(
                    "private export staging directory changed after it was opened"
                )
        except Exception:
            os.close(staging_fd)
            raise
        return name, staging_fd, identity
    raise SnapshotExportApplyError(
        "cannot create a unique private export staging directory"
    ) from last_error


def _list_staging_names(
    staging_fd: int, *, include_writer_lock: bool = False
) -> Tuple[List[str], bool]:
    names: List[str] = []
    saw_lock = False
    try:
        with os.scandir(staging_fd) as iterator:
            for entry in iterator:
                name = entry.name
                if name == EXPORT_STAGING_WRITER_LOCK_NAME:
                    saw_lock = True
                    if include_writer_lock:
                        names.append(name)
                    continue
                if not _is_canonical_direct_name(name):
                    raise SnapshotExportApplyError(
                        f"staging contains a non-canonical direct name: {name!r}"
                    )
                names.append(name)
    except SnapshotExportApplyError:
        raise
    except OSError as error:
        raise SnapshotExportApplyIntegrityError(
            f"cannot list private export staging directory: {error}"
        ) from error
    return names, saw_lock


def _copy_one_payload(
    source_fd: int,
    staging_fd: int,
    record: Mapping[str, Any],
    source_dir: Path,
    owned_files: Dict[str, Tuple[int, int]],
) -> None:
    name = str(record["path"])
    if not _is_canonical_direct_name(name) or name not in ACCEPTED_PAYLOAD_FILES:
        raise SnapshotExportApplyError(
            f"export plan file path is not a direct envelope name: {name!r}"
        )
    expected_size = record["size_bytes"]
    expected_revision = record["content_revision"]
    src_file, before = _open_regular_nofollow(
        source_fd, name, source_dir / name, label=name
    )
    dest_fd: Optional[int] = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            dest_fd = os.open(name, flags, PAYLOAD_FILE_MODE, dir_fd=staging_fd)
        except OSError as error:
            if error.errno == errno.EEXIST:
                raise SnapshotExportApplyError(
                    f"staging payload collision: {name}"
                ) from error
            if error.errno in {errno.ELOOP, errno.ENOENT}:
                raise SnapshotExportApplyIntegrityError(
                    f"staging payload {name} changed or became unsafe"
                ) from error
            raise SnapshotExportApplyError(
                f"cannot exclusively create staging payload {name}: {error}"
            ) from error
        created = os.fstat(dest_fd)
        if not stat.S_ISREG(created.st_mode):
            raise SnapshotExportApplyIntegrityError(
                f"staged payload {name} is not a regular file"
            )
        owned_files[name] = _directory_inode(created)
        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = _read_chunk(src_file, HASH_CHUNK_BYTES)
            except OSError as error:
                raise _io_error("read export payload", error) from error
            if not chunk:
                break
            _write_all(dest_fd, chunk)
            digest.update(chunk)
            total += len(chunk)
        observed = "sha256:" + digest.hexdigest()
        if total != expected_size or observed != expected_revision:
            raise SnapshotExportApplyIntegrityError(
                f"source payload {name} did not match the fresh export plan "
                "while it was copied"
            )
        after_src = os.fstat(src_file)
        try:
            path_src = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotExportApplyIntegrityError(
                f"source payload {name} changed while it was copied"
            ) from error
        if (
            _file_identity(after_src) != _file_identity(before)
            or stat.S_ISLNK(path_src.st_mode)
            or not stat.S_ISREG(path_src.st_mode)
            or _file_identity(path_src) != _file_identity(before)
            or total != before.st_size
        ):
            raise SnapshotExportApplyIntegrityError(
                f"source payload {name} changed while it was copied"
            )
        _fsync_file(dest_fd)
        dest_info = os.fstat(dest_fd)
        if (
            not stat.S_ISREG(dest_info.st_mode)
            or dest_info.st_size != expected_size
            or _directory_inode(dest_info) != owned_files[name]
        ):
            raise SnapshotExportApplyIntegrityError(
                f"staged payload {name} is not the planned regular file"
            )
    finally:
        if dest_fd is not None:
            os.close(dest_fd)
        os.close(src_file)


def _verify_staged_payloads(
    staging_fd: int,
    records: Sequence[Mapping[str, Any]],
    staging_path: Path,
    *,
    expect_writer_lock: bool = False,
    writer_lease: Optional[HeldExportWriterLease] = None,
) -> None:
    present, saw_lock = _list_staging_names(staging_fd)
    planned = [str(item["path"]) for item in records]
    if set(present) != set(planned):
        raise SnapshotExportApplyIntegrityError(
            "private export staging listing is not the planned payload set"
        )
    if expect_writer_lock:
        if not saw_lock:
            raise SnapshotExportApplyIntegrityError(
                "export writer lock disappeared during the staging-write interval"
            )
        if writer_lease is None:
            raise SnapshotExportApplyError(
                "staged verification expected a held export writer lease"
            )
        writer_lease.revalidate()
    elif saw_lock:
        raise SnapshotExportApplyIntegrityError(
            "export writer lock is still present after release"
        )
    for record in records:
        name = str(record["path"])
        _data, revision, identity = _stream_regular_file(
            staging_fd, name, staging_path / name, label=name
        )
        if identity[2] != record["size_bytes"] or revision != record["content_revision"]:
            raise SnapshotExportApplyIntegrityError(
                f"staged payload {name} did not match the fresh export plan"
            )


def _tokens_still_match(
    expected: Mapping[str, Any],
    observed: Mapping[str, Any],
    *,
    requested: str,
    resolved: str,
    source_identity: Tuple[int, int, int, int],
) -> None:
    if (
        expected["lock_identity"] != observed["lock_identity"]
        or expected["current_value"] != observed["current_value"]
        or expected["current_identity"] != observed["current_identity"]
        or expected["listing"] != observed["listing"]
        or expected["selected_identity"] != observed["selected_identity"]
        or observed["selected_identity"] != source_identity
    ):
        raise SnapshotExportApplyIntegrityError(
            "publication lock, current, snapshots listing, or selected "
            "snapshot changed during export apply"
        )
    if requested == CURRENT_REF and observed["current_value"] != resolved:
        raise SnapshotExportApplyIntegrityError(
            "current no longer names the selected snapshot"
        )


def _revalidate_plan_tokens(
    root: Path,
    requested: str,
    resolved: str,
    source_identity: Tuple[int, int, int, int],
    plan_tokens: Mapping[str, Any],
) -> None:
    later = _capture_tokens(root, requested=requested, resolved=resolved)
    _tokens_still_match(
        plan_tokens,
        later,
        requested=requested,
        resolved=resolved,
        source_identity=source_identity,
    )


def _reobserve_source(
    root: Path,
    requested: str,
    resolved: str,
    source_dir: Path,
    source_fd: int,
    source_identity: Tuple[int, int, int, int],
    plan_tokens: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> None:
    _revalidate_plan_tokens(
        root, requested, resolved, source_identity, plan_tokens
    )
    present = _payload_children(source_dir, source_fd, source_identity)
    planned = [str(item["path"]) for item in records]
    if set(present) != set(planned):
        raise SnapshotExportApplyIntegrityError(
            "selected snapshot payload set changed during export apply"
        )
    for record in records:
        name = str(record["path"])
        _data, revision, identity = _stream_regular_file(
            source_fd, name, source_dir / name, label=name
        )
        if (
            identity != _file_identity(present[name])
            or identity[2] != record["size_bytes"]
            or revision != record["content_revision"]
        ):
            raise SnapshotExportApplyIntegrityError(
                f"source payload {name} identity or content changed during export apply"
            )
    # Token observation brackets the complete payload rehash. Otherwise a
    # stable current/lock/listing change during that potentially long pass
    # could become invisible before publication.
    _revalidate_plan_tokens(
        root, requested, resolved, source_identity, plan_tokens
    )


def _cleanup_owned_staging(
    parent_fd: int,
    staging_name: str,
    staging_identity: Tuple[int, int],
    owned_files: Mapping[str, Tuple[int, int]],
    owned_lock: Optional[Tuple[int, int]] = None,
) -> None:
    """Remove only this invocation's staging directory and owned children.

    Never follows a replaced pathname and never recursively deletes an
    unresolved tree. Unexpected children are left in place. Writer-lock
    metadata is removed only when the staging directory, lock pathname,
    and creation identity still match. The final rmdir runs only when
    the pathname, held descriptor, and captured creation identity still
    agree and the directory is empty.
    """
    if not _is_canonical_direct_name(staging_name):
        return
    _before_export_apply_staging_cleanup(parent_fd, staging_name)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        staging_fd = os.open(staging_name, flags, dir_fd=parent_fd)
    except OSError:
        return
    try:
        opened = os.fstat(staging_fd)
        if _directory_inode(opened) != staging_identity:
            return
        if owned_lock is not None:
            cleanup_owned_export_writer_lock(staging_fd, owned_lock)
        try:
            names, _saw_lock = _list_staging_names(
                staging_fd, include_writer_lock=True
            )
        except (SnapshotExportApplyError, OSError):
            return
        for name in names:
            if name == EXPORT_STAGING_WRITER_LOCK_NAME:
                continue
            if name not in owned_files or not _is_canonical_direct_name(name):
                continue
            try:
                info = os.stat(name, dir_fd=staging_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            if (info.st_dev, info.st_ino) != owned_files[name]:
                continue
            try:
                os.unlink(name, dir_fd=staging_fd)
            except OSError:
                continue
        _before_export_apply_staging_rmdir(parent_fd, staging_name)
        try:
            opened = os.fstat(staging_fd)
            path_identity = os.stat(
                staging_name, dir_fd=parent_fd, follow_symlinks=False
            )
        except OSError:
            return
        if (
            _directory_inode(opened) != staging_identity
            or stat.S_ISLNK(path_identity.st_mode)
            or not stat.S_ISDIR(path_identity.st_mode)
            or _directory_inode(path_identity) != staging_identity
        ):
            return
        try:
            remaining, saw_lock = _list_staging_names(
                staging_fd, include_writer_lock=True
            )
        except (SnapshotExportApplyError, OSError):
            return
        if remaining or saw_lock:
            return
    finally:
        os.close(staging_fd)
    try:
        os.rmdir(staging_name, dir_fd=parent_fd)
    except OSError:
        return


def _file_records(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
    files = plan.get("files")
    if not isinstance(files, list):
        raise SnapshotExportApplyError("fresh export plan is missing files")
    records: List[Dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise SnapshotExportApplyError("fresh export plan file record is malformed")
        path = item.get("path")
        size = item.get("size_bytes")
        revision = item.get("content_revision")
        if (
            not isinstance(path, str)
            or not _is_canonical_direct_name(path)
            or path not in ACCEPTED_PAYLOAD_FILES
        ):
            raise SnapshotExportApplyError(
                f"fresh export plan file path is not a direct envelope name: {path!r}"
            )
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SnapshotExportApplyError(
                f"fresh export plan file size is not a non-negative integer: {size!r}"
            )
        if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
            raise SnapshotExportApplyError(
                "fresh export plan content_revision must be sha256:<64 lowercase hex>"
            )
        records.append(
            {
                "path": path,
                "size_bytes": size,
                "content_revision": revision,
            }
        )
    paths = [item["path"] for item in records]
    if len(set(paths)) != len(paths) or paths != sorted(
        paths, key=lambda item: item.encode("utf-8")
    ):
        raise SnapshotExportApplyError(
            "fresh export plan files must be unique and sorted in UTF-8-byte order"
        )
    return records


def _require_staging_name_is_held(
    parent_fd: int,
    staging_name: str,
    staging_fd: int,
    staging_identity: Tuple[int, int],
) -> None:
    held = _directory_inode(os.fstat(staging_fd))
    if held != staging_identity:
        raise SnapshotExportApplyIntegrityError(
            "held export staging descriptor no longer matches its creation identity"
        )
    path_identity = _path_directory_inode(
        parent_fd, staging_name, label="private export staging directory"
    )
    if path_identity != staging_identity:
        raise SnapshotExportApplyIntegrityError(
            "private export staging pathname no longer names the held staging directory"
        )


def _build_result(
    *,
    root: Path,
    requested: str,
    resolved: str,
    destination: Path,
    records: Sequence[Mapping[str, Any]],
    expected: str,
    observed: str,
    ok: bool = True,
    partial: bool = False,
    export_performed: bool = True,
    destination_created: bool = True,
    destination_verified: bool = True,
    source_unchanged: bool = True,
    parent_fsync_confirmed: bool = True,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "ok": ok,
        "partial": partial,
        "graph": str(root),
        "requested_snapshot": requested,
        "resolved_snapshot": resolved,
        "destination": str(destination),
        "files": [dict(item) for item in records],
        "file_count": len(records),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in records),
        "expected_export_revision": expected,
        "observed_export_revision": observed,
        "export_confirmed": True,
        "export_performed": export_performed,
        "destination_created": destination_created,
        "destination_verified": destination_verified,
        "source_unchanged": source_unchanged,
        "parent_fsync_confirmed": parent_fsync_confirmed,
        "error": error,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }


def _apply_unlocked(
    root: Path,
    requested: str,
    expected: str,
    parent_resolved: Path,
    dest_resolved: Path,
    dest_name: str,
) -> Dict[str, Any]:
    _require_managed_graph(root)
    _require_export_apply_primitives()
    published = False
    parent_fd: Optional[int] = None
    staging_fd: Optional[int] = None
    staging_name: Optional[str] = None
    staging_identity: Optional[Tuple[int, int]] = None
    owned_files: Dict[str, Tuple[int, int]] = {}
    writer_lease: Optional[HeldExportWriterLease] = None
    owned_lock: Optional[Tuple[int, int]] = None
    try:
        with _held_snapshot_export_plan_unlocked(root, requested) as (
            plan,
            source_fd,
            source_identity,
            plan_tokens,
        ):
            observed = plan.get("export_revision")
            if not isinstance(observed, str) or observed != expected:
                raise SnapshotExportApplyError(
                    "expected export_revision no longer matches the freshly "
                    f"recomputed plan: {observed!r} != {expected!r}"
                )
            records = _file_records(plan)
            resolved = str(plan["resolved_snapshot"])
            source_dir = root / "snapshots" / resolved
            if plan_tokens["selected_identity"] != source_identity:
                raise SnapshotExportApplyIntegrityError(
                    "selected snapshot directory changed before export copy"
                )
            if requested == CURRENT_REF and plan_tokens["current_value"] != resolved:
                raise SnapshotExportApplyIntegrityError(
                    "current no longer names the selected snapshot"
                )
            _after_export_apply_plan_computed(root, plan)
            _revalidate_plan_tokens(
                root, requested, resolved, source_identity, plan_tokens
            )
            parent_fd, parent_identity = _open_dest_parent(parent_resolved)
            _child_absent(parent_fd, dest_name, label="destination")
            staging_name, staging_fd, staging_identity = _create_private_staging(
                parent_fd
            )
            try:
                writer_lease = acquire_export_writer_lease(
                    parent_fd=parent_fd,
                    staging_name=staging_name,
                    staging_fd=staging_fd,
                    staging_identity=staging_identity,
                    staging_path=parent_resolved / staging_name,
                )
            except ExportWriterLeaseError as error:
                raise _wrap_writer_lease_error(error) from error
            owned_lock = writer_lease.inode_identity
            _after_export_apply_writer_lease(
                parent_fd, staging_name, staging_fd, writer_lease.fd
            )
            _after_export_apply_staging_created(parent_fd, staging_name, staging_fd)
            for record in records:
                _copy_one_payload(
                    source_fd, staging_fd, record, source_dir, owned_files
                )
            _fsync_directory(staging_fd)
            _after_export_apply_copied(root, records)
            _verify_staged_payloads(
                staging_fd,
                records,
                parent_resolved / staging_name,
                expect_writer_lock=True,
                writer_lease=writer_lease,
            )
            _after_export_apply_staged_verified(root, records)
            _reobserve_source(
                root,
                requested,
                resolved,
                source_dir,
                source_fd,
                source_identity,
                plan_tokens,
                records,
            )
            source_unchanged = True
            _after_export_apply_source_reobserved(root, records)
            _verify_staged_payloads(
                staging_fd,
                records,
                parent_resolved / staging_name,
                expect_writer_lock=True,
                writer_lease=writer_lease,
            )
            _before_export_apply_writer_lease_release(
                parent_fd, staging_name, staging_fd
            )
            _require_destination_parent_is_held(
                parent_resolved, parent_fd, parent_identity
            )
            _require_staging_name_is_held(
                parent_fd, staging_name, staging_fd, staging_identity
            )
            try:
                writer_lease.revalidate()
                writer_lease.release_and_remove()
            except ExportWriterLeaseError as error:
                raise _wrap_writer_lease_error(error) from error
            writer_lease = None
            _fsync_directory(staging_fd)
            try:
                prove_export_writer_lock_absent(
                    staging_fd, parent_resolved / staging_name
                )
            except ExportWriterLeaseError as error:
                raise _wrap_writer_lease_error(error) from error
            _after_export_apply_writer_lease_removed(
                parent_fd, staging_name, staging_fd
            )
            _verify_staged_payloads(
                staging_fd,
                records,
                parent_resolved / staging_name,
                expect_writer_lock=False,
            )
            _before_export_apply_publication(parent_fd, dest_name, staging_name)
            _require_destination_parent_is_held(
                parent_resolved, parent_fd, parent_identity
            )
            _require_staging_name_is_held(
                parent_fd, staging_name, staging_fd, staging_identity
            )
            try:
                _publish_staging(parent_fd, staging_name, dest_name)
            except RenameNoreplaceError as error:
                if error.errno == errno.EEXIST:
                    raise SnapshotExportApplyError(
                        f"destination already exists: {dest_resolved}"
                    ) from error
                raise SnapshotExportApplyError(
                    f"atomic destination publication failed: {error}"
                ) from error
            published = True
            _after_export_apply_published(parent_fd, dest_name)
            destination_verified = False
            parent_fsync_confirmed = False
            errors: List[str] = []
            try:
                _require_destination_parent_is_held(
                    parent_resolved, parent_fd, parent_identity
                )
                dest_inode = _path_directory_inode(
                    parent_fd, dest_name, label="export destination"
                )
                held = _directory_inode(os.fstat(staging_fd))
                if dest_inode != staging_identity or held != staging_identity:
                    errors.append(
                        "published destination is not the held export staging directory"
                    )
                else:
                    try:
                        _verify_staged_payloads(staging_fd, records, dest_resolved)
                        _require_destination_parent_is_held(
                            parent_resolved, parent_fd, parent_identity
                        )
                        after_dest_inode = _path_directory_inode(
                            parent_fd, dest_name, label="export destination"
                        )
                        after_held = _directory_inode(os.fstat(staging_fd))
                        if (
                            after_dest_inode != staging_identity
                            or after_held != staging_identity
                        ):
                            errors.append(
                                "published destination changed during verification"
                            )
                        else:
                            destination_verified = True
                    except (
                        SnapshotExportApplyError,
                        SnapshotExportPlanError,
                        OSError,
                    ) as error:
                        errors.append(str(error))
            except (
                SnapshotExportApplyError,
                SnapshotExportPlanError,
                OSError,
            ) as error:
                errors.append(str(error))
            try:
                _fsync_directory(parent_fd)
                parent_fsync_confirmed = True
            except SnapshotExportApplyError as error:
                errors.append(str(error))
            if destination_verified:
                try:
                    _require_destination_parent_is_held(
                        parent_resolved, parent_fd, parent_identity
                    )
                    final_dest_inode = _path_directory_inode(
                        parent_fd, dest_name, label="export destination"
                    )
                    final_held = _directory_inode(os.fstat(staging_fd))
                    if (
                        final_dest_inode != staging_identity
                        or final_held != staging_identity
                    ):
                        destination_verified = False
                        errors.append(
                            "published destination changed after verification"
                        )
                except (
                    SnapshotExportApplyError,
                    SnapshotExportPlanError,
                    OSError,
                ) as error:
                    destination_verified = False
                    errors.append(str(error))
            if destination_verified and parent_fsync_confirmed:
                return _build_result(
                    root=root,
                    requested=requested,
                    resolved=resolved,
                    destination=dest_resolved,
                    records=records,
                    expected=expected,
                    observed=str(observed),
                    ok=True,
                    partial=False,
                    destination_verified=True,
                    source_unchanged=source_unchanged,
                    parent_fsync_confirmed=True,
                )
            return _build_result(
                root=root,
                requested=requested,
                resolved=resolved,
                destination=dest_resolved,
                records=records,
                expected=expected,
                observed=str(observed),
                ok=False,
                partial=True,
                destination_verified=destination_verified,
                source_unchanged=source_unchanged,
                parent_fsync_confirmed=parent_fsync_confirmed,
                error=_bound_error("; ".join(errors) or "post-publication verification failed"),
            )
    except SnapshotExportPlanError as error:
        raise _wrap_plan_error(error) from error
    except SnapshotStagingError as error:
        raise _wrap_staging_error_as_apply(error) from error
    except ExportWriterLeaseError as error:
        raise _wrap_writer_lease_error(error) from error
    finally:
        if writer_lease is not None:
            writer_lease.close()
        if (
            not published
            and parent_fd is not None
            and staging_name is not None
            and staging_identity is not None
        ):
            _cleanup_owned_staging(
                parent_fd,
                staging_name,
                staging_identity,
                owned_files,
                owned_lock=owned_lock,
            )
        if staging_fd is not None:
            try:
                os.close(staging_fd)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


@contextmanager
def _snapshot_export_apply_scope(
    graph: Path,
    snapshot: object,
    destination: object,
    expected_export_revision: object,
    *,
    export_confirmed: bool,
) -> Iterator[Dict[str, Any]]:
    """Yield one apply result while its shared existing-lock lease remains held."""
    if not export_confirmed:
        raise SnapshotExportApplyError(_CONFIRMATION_MESSAGE)
    try:
        requested = _parse_snapshot(snapshot)
    except SnapshotExportPlanError as error:
        raise _wrap_plan_error(error) from error
    expected = parse_export_revision(expected_export_revision)
    _require_export_apply_primitives()
    try:
        root = _resolve_graph_root(graph)
    except SnapshotExportPlanError as error:
        raise _wrap_plan_error(error) from error
    parent_resolved, dest_resolved, dest_name = _resolve_destination(destination)
    try:
        with graph_read_lease(root, allow_unlocked_managed=False):
            yield _apply_unlocked(
                root,
                requested,
                expected,
                parent_resolved,
                dest_resolved,
                dest_name,
            )
    except SnapshotExportApplyError:
        raise
    except SnapshotExportPlanError as error:
        raise _wrap_plan_error(error) from error
    except SnapshotStagingError as error:
        raise _wrap_staging_error_as_apply(error) from error
    except ExportWriterLeaseError as error:
        raise _wrap_writer_lease_error(error) from error
    except ByogPublicationLockError as error:
        raise _apply_lock_error(error) from error
    except ByogReaderLockError as error:
        raise _apply_lock_error(error) from error


def snapshot_export_apply(
    graph: Path,
    snapshot: str,
    destination: Path,
    expected_export_revision: str,
    *,
    export_confirmed: bool,
) -> Dict[str, Any]:
    """Copy one CAS-verified snapshot envelope into a new destination."""
    with _snapshot_export_apply_scope(
        graph,
        snapshot,
        destination,
        expected_export_revision,
        export_confirmed=export_confirmed,
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Copy one retained published snapshot's direct envelope payload "
            "files into a newly created destination directory. Requires "
            "--export-confirmed and --expected-export-revision. "
            "snapshot-export-plan is the preview; this command has no "
            "dry-run. Does not mutate the graph, overwrite a pre-existing "
            "destination, create an archive, or claim backup or "
            "recoverability. Never creates .publish.lock, and is not an "
            "MCP tool. A crash may leave the private sibling staging "
            "directory and its .export-writer.lock protocol file."
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
        "--snapshot",
        required=True,
        help="current or a canonical retained published snapshot id.",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="Absent directory that will receive the copied payload files.",
    )
    parser.add_argument(
        "--expected-export-revision",
        required=True,
        help="sha256:<64 lowercase hex> from a fresh snapshot-export-plan",
    )
    parser.add_argument(
        "--export-confirmed",
        action="store_true",
        help=(
            "Required to create the destination directory. The command "
            "still refuses to copy if the recomputed export_revision no "
            "longer matches."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_export_apply_scope(
            args.graph,
            args.snapshot,
            args.destination,
            args.expected_export_revision,
            export_confirmed=bool(args.export_confirmed),
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing the graph lease so
            # the complete response is handed to the caller under that lease.
            sys.stdout.flush()
            if result.get("partial") or not result.get("ok"):
                return 1
    except SnapshotExportApplyError as error:
        print(f"snapshot-export-apply: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-export-apply: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
