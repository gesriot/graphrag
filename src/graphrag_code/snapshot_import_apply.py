#!/usr/bin/env python
"""CAS-guarded snapshot import apply.

``snapshot-import-apply`` publishes one validated standalone snapshot
export as a retained snapshot in an existing managed BYOG graph. It
preserves the source manifest id exactly, copies the exact source
envelope bytes into ``.staging-<source-manifest-id>``, and atomically
promotes that private staging directory without replacing any existing
entry. It does not change ``current``, operator pins, claim pins, or
run retention. It does not overwrite, merge with, repair, or compare an
existing snapshot id. It never mutates the standalone export, never
creates or adopts ``.publish.lock``, and is CLI-only.

Successful import means only that the language-independent persisted
snapshot envelope and observed bytes were copied and atomically added
to retained history. It is not authenticity, provenance, backup
quality, portability, recoverability, restore success, or semantic
equivalence. A later activation remains a separate explicit
``snapshot-activate --activate-confirmed`` operation.

``--expected-import-revision`` is a compare-and-swap token. Apply
reproduces the current schema-1 import-plan contract from held source
descriptors and one shared existing-lock target lease, then copies only
when that token still matches and the plan is import-ready with both
the final id and the exact staging name absent. A stale or blocked
revision creates nothing.

The unavoidable directory-creation-to-writer-lock window remains
unverifiable. ``.staging-writer.lock`` is protocol metadata, not
ownership or liveness evidence. A crash may leave ``.staging-<id>``
and its persistent writer-lock metadata. Existing staging
inventory/cleanup commands are the only later cleanup path. After
native publication succeeds, a later reporting failure never deletes
the published snapshot.

Usage:
    graphrag-code snapshot-import-apply --graph <managed-root> \\
        --export-dir <standalone-export-directory> \\
        --expected-import-revision sha256:<hex> --import-confirmed [--json]
    python -m graphrag_code.snapshot_import_apply --graph <managed-root> \\
        --export-dir <standalone-export-directory> \\
        --expected-import-revision sha256:<hex> --import-confirmed [--json]
    uv run python scripts/snapshot_import_apply.py --graph <managed-root> \\
        --export-dir <standalone-export-directory> \\
        --expected-import-revision sha256:<hex> --import-confirmed [--json]
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
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from graphrag_code._rename_noreplace import (
    RenameNoreplaceError,
    rename_directory_noreplace,
    rename_noreplace_supported,
)
from graphrag_code.byog_graph import (
    PUBLICATION_LOCK_NAME,
    STAGING_NAME_PREFIX,
    STAGING_WRITER_LOCK_NAME,
    ByogPublicationLockError,
    ByogReaderLockError,
    StagingWriterLeaseError,
    StagingWriterLockContention,
    StagingWriterLockUnsafe,
    _available_lock_backend,
    acquire_existing_staging_writer_claim,
    graph_exclusive_lease,
    graph_read_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
    staging_writer_lease,
)
from graphrag_code.byog_snapshot_integrity import MANIFEST_NAME
from graphrag_code.snapshot_export_plan import (
    HASH_CHUNK_BYTES,
    MAX_MANIFEST_BYTES,
    PLAN_SCHEMA_VERSION,
    SnapshotExportPlanError,
    SnapshotExportPlanIntegrityError,
    _is_canonical_direct_name,
    _require_descriptor_reads,
    export_revision_of,
)
from graphrag_code.snapshot_import_plan import (
    SnapshotImportPlanError,
    SnapshotImportPlanIntegrityError,
    _byte_sort,
    _complete_directory_identity,
    _complete_file_identity,
    _from_export_error,
    _hash_held_fd,
    _held_standalone_export_observation,
    _listing_token,
    _lock_error,
    _observe_fresh_import_plan,
    _observe_held_directory,
    _observe_listing,
    _open_anchored_graph,
    _parse_manifest,
    _read_held_bytes,
    _reobserve_held_payload,
    _reobserve_held_source,
    _require_managed_graph,
    _require_path_identity,
    _resolve_export_dir,
    _resolve_graph_root,
    _validate_source_envelope,
    _wrap_staging_error,
)
from graphrag_code.snapshot_staging import SnapshotStagingError
from graphrag_code.snapshot_staging import (
    _list_snapshot_entries as _list_target_snapshot_entries,
    _lock_identity as _target_lock_identity,
    _read_current as _read_target_current,
)

APPLY_SCHEMA_VERSION = 1
STAGING_DIR_MODE = 0o700
PAYLOAD_FILE_MODE = 0o600
_MAX_ERROR_CHARS = 400
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_CONFIRMATION_MESSAGE = """\
refusing to apply snapshot import without --import-confirmed.

This is an explicit CLI publication of one standalone snapshot export
as a retained snapshot in an existing managed graph.
snapshot-import-plan is the preview; this command has no dry-run.
Confirmation is required even when the planned action is empty or
impossible. The copy is not a backup, archive, restore kit, or
authorization to delete anything. It does not activate current, pin,
prune, or run retention.

--expected-import-revision is a compare-and-swap guard: one shared
existing-lock lease recomputes a fresh import plan and copies only
when that token still matches and the plan is import-ready. A
mismatched or blocked revision creates nothing. A crash may leave
.staging-<snapshot-id> and its .staging-writer.lock protocol file.
Existing staging inventory/cleanup commands are the only later
cleanup path.
""".strip()
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "import_is_not_backup",
        "kind": "notice",
        "message": (
            "Successful import copies the language-independent persisted "
            "snapshot envelope into retained history. It is not a backup, "
            "archive, restore kit, authentic replica, or recoverable image, "
            "and it is not authorization to delete anything."
        ),
    },
    {
        "code": "import_revision_is_cas_only",
        "kind": "notice",
        "message": (
            "expected_import_revision is a compare-and-swap token for the "
            "freshly reproduced import plan. It does not prove provenance, "
            "authenticity, portability, or recoverability."
        ),
    },
    {
        "code": "import_is_not_activation",
        "kind": "notice",
        "message": (
            "This command leaves current unchanged. A later activation "
            "remains a separate explicit snapshot-activate "
            "--activate-confirmed operation."
        ),
    },
    {
        "code": "crash_may_leave_private_staging",
        "kind": "notice",
        "message": (
            "A crash before atomic publication may leave "
            ".staging-<snapshot-id> and, if created, the regular "
            ".staging-writer.lock protocol file. Process death releases "
            "the kernel lease but does not remove that file. Existing "
            "staging inventory and cleanup commands are the only later "
            "cleanup path. After publication succeeds the published "
            "snapshot is never automatically deleted."
        ),
    },
    {
        "code": "staging_writer_lease_not_ownership",
        "kind": "notice",
        "message": (
            "The private .staging-writer.lock file is protocol metadata, "
            "not proof of ownership, writer identity, writer death, "
            "crash, or cleanup eligibility. The unavoidable "
            "directory-creation-to-writer-lock window remains "
            "unverifiable. The pathname is removed through the held "
            "lease release_and_remove protocol while the graph lease is "
            "exclusive, before atomic publication."
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
        "code": "source_envelope_language_independent_only",
        "kind": "notice",
        "message": (
            "This command proves only the language-independent stored "
            "snapshot envelope and observed bytes. It does not compare "
            "source_root, git_commit, or created_at with the current host, "
            "and it does not run any language-specific or Clang overlay audit."
        ),
    },
    {
        "code": "cli_only_not_mcp",
        "kind": "notice",
        "message": (
            "snapshot-import-apply is CLI-only and intentionally absent "
            "from the fixed 13-tool MCP set."
        ),
    },
)


class SnapshotImportApplyError(Exception):
    """Expected import-apply refusal. Default exit 2."""

    exit_code = 2


class SnapshotImportApplyIntegrityError(SnapshotImportApplyError):
    """Unsafe structure, CAS mismatch, or concurrent change. Exit 1."""

    exit_code = 1


def parse_import_revision(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotImportApplyError(
            "expected-import-revision must be sha256:<64 lowercase hex>"
        )
    if value != value.strip():
        raise SnapshotImportApplyError(
            "expected-import-revision must not contain leading or trailing "
            f"whitespace: {value!r}"
        )
    if not _REVISION_RE.fullmatch(value):
        raise SnapshotImportApplyError(
            "expected-import-revision must be sha256:<64 lowercase hex>, "
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
        "snapshot-import-apply: "
        f"graph={result.get('graph')} "
        f"export_directory={result.get('export_directory')} "
        f"snapshot_id={result.get('snapshot_id')} "
        f"files={result.get('file_count')} "
        f"total_size_bytes={result.get('total_size_bytes')} "
        f"expected_import_revision={result.get('expected_import_revision')} "
        f"observed_import_revision={result.get('observed_import_revision')} "
        f"ok={str(bool(result.get('ok'))).lower()} "
        f"partial={str(bool(result.get('partial'))).lower()} "
        f"import_performed={str(bool(result.get('import_performed'))).lower()} "
        f"publication_performed={str(bool(result.get('publication_performed'))).lower()} "
        f"current_unchanged={str(bool(result.get('current_unchanged'))).lower()} "
        f"staging_created={str(bool(result.get('staging_created'))).lower()} "
        f"snapshots_fsync_confirmed={str(bool(result.get('snapshots_fsync_confirmed'))).lower()}"
    )
    error = result.get("error")
    if isinstance(error, str) and error:
        text += f" error={error}"
    return (
        text
        + " This copy is not a backup, not an activation, and is not "
        "authorization to delete anything."
    )


def _wrap_plan_error(error: Exception) -> SnapshotImportApplyError:
    if isinstance(error, SnapshotImportPlanIntegrityError):
        return SnapshotImportApplyIntegrityError(str(error))
    if isinstance(error, SnapshotImportPlanError):
        wrapped = SnapshotImportApplyError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    if isinstance(error, SnapshotExportPlanIntegrityError):
        return SnapshotImportApplyIntegrityError(str(error))
    if isinstance(error, SnapshotExportPlanError):
        wrapped = _from_export_error(error)
        if isinstance(wrapped, SnapshotImportPlanIntegrityError):
            return SnapshotImportApplyIntegrityError(str(wrapped))
        out = SnapshotImportApplyError(str(wrapped))
        out.exit_code = getattr(wrapped, "exit_code", 2)
        return out
    if isinstance(error, SnapshotStagingError):
        wrapped = _wrap_staging_error(error)
        if isinstance(wrapped, SnapshotImportPlanIntegrityError):
            return SnapshotImportApplyIntegrityError(str(wrapped))
        out = SnapshotImportApplyError(str(wrapped))
        out.exit_code = getattr(wrapped, "exit_code", 2)
        return out
    return SnapshotImportApplyError(str(error))


def _apply_lock_error(error: Exception) -> SnapshotImportApplyError:
    wrapped = _lock_error(error)
    if isinstance(wrapped, SnapshotImportPlanIntegrityError):
        return SnapshotImportApplyIntegrityError(str(wrapped))
    if "publication lock is missing" in str(error):
        return SnapshotImportApplyError(f"{error}\n{_MISSING_LOCK_HINT}")
    out = SnapshotImportApplyError(str(wrapped))
    out.exit_code = getattr(wrapped, "exit_code", 2)
    return out


def _wrap_after_staging_error(error: Exception) -> SnapshotImportApplyError:
    """Normalize any ordinary failure after successful staging mkdir."""
    if isinstance(error, SnapshotImportApplyError):
        return error
    if isinstance(error, StagingWriterLeaseError):
        return _wrap_writer_lease_error(error)
    if isinstance(error, (ByogPublicationLockError, ByogReaderLockError)):
        return _apply_lock_error(error)
    if isinstance(
        error,
        (
            SnapshotImportPlanError,
            SnapshotExportPlanError,
            SnapshotStagingError,
        ),
    ):
        return _wrap_plan_error(error)
    if isinstance(error, OSError):
        return _io_error("snapshot import apply", error)
    return SnapshotImportApplyError(str(error))


def _wrap_writer_lease_error(error: StagingWriterLeaseError) -> SnapshotImportApplyError:
    if isinstance(error, (StagingWriterLockUnsafe, StagingWriterLockContention)):
        return SnapshotImportApplyIntegrityError(str(error))
    message = str(error)
    if "unsupported" in message.lower():
        return SnapshotImportApplyError(message)
    return SnapshotImportApplyIntegrityError(message)


def _require_import_apply_primitives() -> None:
    try:
        _require_descriptor_reads()
    except SnapshotExportPlanError as error:
        raise SnapshotImportApplyError(str(error)) from error
    supported = getattr(os, "supports_dir_fd", set())
    if (
        os.mkdir not in supported
        or os.unlink not in supported
        or os.rmdir not in supported
        or os.open not in supported
        or os.stat not in supported
    ):
        raise SnapshotImportApplyError(
            "safe descriptor-relative exclusive import publication is "
            f"unsupported on this platform: {sys.platform!r}"
        )
    if not rename_noreplace_supported():
        raise SnapshotImportApplyError(
            "atomic no-replace directory publication is unsupported on "
            f"this platform: {sys.platform!r}"
        )
    if _available_lock_backend() is None:
        raise SnapshotImportApplyError(
            "advisory staging-writer lock is unsupported on "
            f"{sys.platform!r}; refusing to guess writer-lease state"
        )


def _read_chunk(fd: int, size: int) -> bytes:
    return os.read(fd, size)


def _write_chunk(fd: int, data: bytes) -> int:
    return os.write(fd, data)


def _fsync(fd: int) -> None:
    os.fsync(fd)


def _io_error(action: str, error: BaseException) -> SnapshotImportApplyError:
    return SnapshotImportApplyError(f"{action} failed: {error}")


def _bound_error(message: object) -> str:
    text = " ".join(str(message).split())
    if not text:
        text = "import apply failed"
    if len(text) > _MAX_ERROR_CHARS:
        return text[:_MAX_ERROR_CHARS]
    return text


def _fsync_file(fd: int) -> None:
    try:
        _fsync(fd)
    except OSError as error:
        raise _io_error("fsync import payload", error) from error


def _fsync_staging_directory(fd: int) -> None:
    try:
        _fsync(fd)
    except OSError as error:
        raise _io_error("fsync import staging directory", error) from error


def _fsync_snapshots_directory(fd: int) -> None:
    try:
        _fsync(fd)
    except OSError as error:
        raise _io_error("fsync snapshots directory", error) from error


def _publish_staging(
    snapshots_fd: int, staging_name: str, snapshot_id: str
) -> None:
    rename_directory_noreplace(
        snapshots_fd, staging_name, snapshots_fd, snapshot_id
    )


def _after_import_apply_plan_computed(
    _root: Path, _plan: Mapping[str, Any]
) -> None:
    """Test hook after the fresh plan and CAS, before staging creation."""
    return None


def _after_import_apply_shared_lease_released(
    _root: Path, _snapshots_fd: int
) -> None:
    """Test hook after the shared graph lease is released and before mkdir."""
    return None


def _after_import_apply_snapshots_path_inspected(
    _graph_path: Path, _graph_fd: int
) -> None:
    """Test hook after initial snapshots lstat and before descriptor open."""
    return None


def _after_import_apply_staging_mkdir(
    _snapshots_fd: int, _staging_name: str
) -> None:
    """Test hook after mkdir and first identity capture, before open."""
    return None


def _after_import_apply_staging_opened(
    _snapshots_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    """Test hook after the staging descriptor is accepted."""
    return None


def _after_import_apply_writer_lease(
    _snapshots_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    """Test hook after the exclusive staging-writer lease is held."""
    return None


def _before_import_apply_copy(
    _root: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook immediately before payload copies."""
    return None


def _after_import_apply_copied(
    _root: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after payload copies and before staged verification."""
    return None


def _after_import_apply_staged_verified(
    _root: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after staged verification and before source reobservation."""
    return None


def _after_import_apply_source_reobserved(
    _root: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after source reobservation and before the exclusive lease."""
    return None


def _before_import_apply_exclusive_lease(
    _root: Path, _staging_name: str
) -> None:
    """Test hook immediately before waiting for the exclusive graph lease."""
    return None


def _after_import_apply_exclusive_lease(
    _root: Path, _staging_name: str
) -> None:
    """Test hook after the exclusive graph lease is held."""
    return None


def _before_import_apply_writer_lock_remove(
    _snapshots_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    """Test hook immediately before writer-lock release_and_remove."""
    return None


def _after_import_apply_writer_lock_removed(
    _snapshots_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    """Test hook after writer-lock removal and before envelope re-verify."""
    return None


def _before_import_apply_publication(
    _snapshots_fd: int, _snapshot_id: str, _staging_name: str
) -> None:
    """Test hook immediately before the atomic no-replace publish."""
    return None


def _after_import_apply_published(
    _snapshots_fd: int, _snapshot_id: str
) -> None:
    """Test hook after native rename returns and before dest identity proof."""
    return None


def _after_import_apply_post_publication_target_observed(
    _root: Path, _snapshot_id: str
) -> None:
    """Test hook after final target observation and before source recheck."""
    return None


def _before_import_apply_staging_cleanup(
    _snapshots_fd: int, _staging_name: str
) -> None:
    """Test hook at the start of best-effort owned staging cleanup."""
    return None


def _before_import_apply_staging_rmdir(
    _snapshots_fd: int, _staging_name: str
) -> None:
    """Test hook immediately before the proven-empty staging rmdir."""
    return None


def _after_import_apply_result_ready(
    _export_dir: Path,
    _graph: Path,
    _export_fd: int,
    _graph_fd: int,
    _snapshots_fd: int,
    _staging_fd: Optional[int],
    _payload_fds: Mapping[str, int],
    _exclusive_held: bool,
    _result: Mapping[str, Any],
) -> None:
    """Test hook after the result is built and before it is yielded."""
    return None


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        try:
            written = _write_chunk(fd, data[offset:])
        except OSError as error:
            raise _io_error("write import payload", error) from error
        if written <= 0:
            raise SnapshotImportApplyError("short write while copying import payload")
        offset += written


def _directory_inode(info: os.stat_result) -> Tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _file_inode(info: os.stat_result) -> Tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _child_absent(directory_fd: int, name: str, *, label: str) -> None:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as error:
        raise SnapshotImportApplyError(
            f"cannot inspect {label} {name!r}: {error}"
        ) from error
    raise SnapshotImportApplyIntegrityError(f"{label} already exists: {name}")


def _path_child_inode(
    directory_fd: int, name: str, *, label: str
) -> Tuple[int, int]:
    try:
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise SnapshotImportApplyIntegrityError(
            f"{label} disappeared: {name}"
        ) from error
    except OSError as error:
        raise SnapshotImportApplyError(
            f"cannot inspect {label} {name}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotImportApplyIntegrityError(f"unsafe symlinked {label}: {name}")
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotImportApplyIntegrityError(
            f"{label} is not a directory: {name}"
        )
    return _directory_inode(info)


def _pathname_present(directory_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _require_held_directory_inode(
    path: Path,
    directory_fd: int,
    expected_inode: Tuple[int, int],
    *,
    label: str,
) -> None:
    try:
        held = os.fstat(directory_fd)
        current = path.lstat()
    except OSError as error:
        raise SnapshotImportApplyIntegrityError(
            f"{label} changed while import was in progress: {path}"
        ) from error
    if (
        not stat.S_ISDIR(held.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _directory_inode(held) != expected_inode
        or _directory_inode(current) != expected_inode
    ):
        raise SnapshotImportApplyIntegrityError(
            f"{label} no longer names the held directory: {path}"
        )


def _require_staging_name_is_held(
    snapshots_fd: int,
    staging_name: str,
    staging_fd: int,
    staging_identity: Tuple[int, int],
) -> None:
    held = _directory_inode(os.fstat(staging_fd))
    if held != staging_identity:
        raise SnapshotImportApplyIntegrityError(
            "held import staging descriptor no longer matches its creation identity"
        )
    path_identity = _path_child_inode(
        snapshots_fd, staging_name, label="import staging directory"
    )
    if path_identity != staging_identity:
        raise SnapshotImportApplyIntegrityError(
            "import staging pathname no longer names the held staging directory"
        )


def _open_anchored_snapshots(
    graph_path: Path,
    graph_fd: int,
    graph_inode: Tuple[int, int],
) -> Tuple[Path, int, Tuple[int, int]]:
    name = "snapshots"
    path = graph_path / name
    try:
        before = os.stat(name, dir_fd=graph_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise SnapshotImportApplyError(
            f"snapshots directory does not exist: {path}"
        ) from error
    except OSError as error:
        raise SnapshotImportApplyError(
            f"cannot inspect snapshots directory {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotImportApplyIntegrityError(
            f"snapshots directory must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotImportApplyIntegrityError(
            f"snapshots directory is not a real directory: {path}"
        )
    before_identity = _complete_directory_identity(before)
    _after_import_apply_snapshots_path_inspected(graph_path, graph_fd)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=graph_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise SnapshotImportApplyIntegrityError(
                f"snapshots directory changed or became unsafe while opening it: {path}"
            ) from error
        raise SnapshotImportApplyError(
            f"cannot safely open snapshots directory {path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        current = os.stat(name, dir_fd=graph_fd, follow_symlinks=False)
        path_info = path.lstat()
        inode = _directory_inode(opened)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or _directory_inode(current) != inode
            or stat.S_ISLNK(path_info.st_mode)
            or not stat.S_ISDIR(path_info.st_mode)
            or _directory_inode(path_info) != inode
            or _complete_directory_identity(opened) != before_identity
            or _complete_directory_identity(current) != before_identity
            or _complete_directory_identity(path_info) != before_identity
        ):
            raise SnapshotImportApplyIntegrityError(
                f"snapshots directory changed or became unsafe while opening it: {path}"
            )
        _require_held_directory_inode(
            graph_path, graph_fd, graph_inode, label="graph root"
        )
        return path, fd, inode
    except Exception:
        os.close(fd)
        raise


def _mkdir_exact_staging(snapshots_fd: int, staging_name: str) -> None:
    if not _is_canonical_direct_name(staging_name) or not is_staging_snapshot_name(
        staging_name
    ):
        raise SnapshotImportApplyError(
            f"staging name is not the exact private import staging name: {staging_name!r}"
        )
    try:
        os.mkdir(staging_name, STAGING_DIR_MODE, dir_fd=snapshots_fd)
    except FileExistsError as error:
        raise SnapshotImportApplyIntegrityError(
            f"import staging pathname already exists: {staging_name}"
        ) from error
    except OSError as error:
        raise SnapshotImportApplyError(
            f"cannot create import staging directory: {error}"
        ) from error


def _capture_created_staging_identity(
    snapshots_fd: int, staging_name: str
) -> Tuple[int, int]:
    try:
        created = os.stat(staging_name, dir_fd=snapshots_fd, follow_symlinks=False)
    except OSError as error:
        raise SnapshotImportApplyError(
            f"cannot inspect import staging directory {staging_name}: {error}"
        ) from error
    if stat.S_ISLNK(created.st_mode) or not stat.S_ISDIR(created.st_mode):
        raise SnapshotImportApplyIntegrityError(
            f"import staging pathname is not a real directory: {staging_name}"
        )
    identity = _directory_inode(created)
    _after_import_apply_staging_mkdir(snapshots_fd, staging_name)
    return identity


def _open_exact_staging(
    snapshots_fd: int, staging_name: str, identity: Tuple[int, int]
) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        staging_fd = os.open(staging_name, flags, dir_fd=snapshots_fd)
    except OSError as error:
        raise SnapshotImportApplyError(
            f"cannot open import staging directory: {error}"
        ) from error
    try:
        opened = os.fstat(staging_fd)
        path_identity = _path_child_inode(
            snapshots_fd, staging_name, label="import staging directory"
        )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or _directory_inode(opened) != identity
            or path_identity != identity
        ):
            raise SnapshotImportApplyIntegrityError(
                "import staging directory changed while opening it"
            )
    except Exception:
        os.close(staging_fd)
        raise
    _after_import_apply_staging_opened(snapshots_fd, staging_name, staging_fd)
    opened = os.fstat(staging_fd)
    path_identity = _path_child_inode(
        snapshots_fd, staging_name, label="import staging directory"
    )
    if _directory_inode(opened) != identity or path_identity != identity:
        os.close(staging_fd)
        raise SnapshotImportApplyIntegrityError(
            "import staging directory changed after it was opened"
        )
    return staging_fd


def _list_staging_names(
    staging_fd: int, *, include_writer_lock: bool = False
) -> Tuple[List[str], bool]:
    names: List[str] = []
    saw_lock = False
    try:
        with os.scandir(staging_fd) as iterator:
            for entry in iterator:
                name = entry.name
                if name == STAGING_WRITER_LOCK_NAME:
                    saw_lock = True
                    if include_writer_lock:
                        names.append(name)
                    continue
                if not _is_canonical_direct_name(name):
                    raise SnapshotImportApplyError(
                        f"staging contains a non-canonical direct name: {name!r}"
                    )
                names.append(name)
    except SnapshotImportApplyError:
        raise
    except OSError as error:
        raise SnapshotImportApplyIntegrityError(
            f"cannot list import staging directory: {error}"
        ) from error
    return names, saw_lock


def _copy_one_payload(
    source_fd: int,
    source_dir_fd: int,
    staging_fd: int,
    record: Mapping[str, Any],
    expected_identity: Tuple[int, int, int, int, int, int],
    owned_files: Dict[str, Tuple[int, int]],
) -> None:
    name = str(record["path"])
    if not _is_canonical_direct_name(name):
        raise SnapshotImportApplyError(
            f"import plan file path is not a canonical direct name: {name!r}"
        )
    expected_size = record["size_bytes"]
    expected_revision = record["content_revision"]
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
    except OSError as error:
        raise SnapshotImportApplyIntegrityError(
            f"cannot rewind source payload {name}: {error}"
        ) from error
    dest_fd: Optional[int] = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            dest_fd = os.open(name, flags, PAYLOAD_FILE_MODE, dir_fd=staging_fd)
        except OSError as error:
            if error.errno == errno.EEXIST:
                raise SnapshotImportApplyError(
                    f"staging payload collision: {name}"
                ) from error
            if error.errno in {errno.ELOOP, errno.ENOENT}:
                raise SnapshotImportApplyIntegrityError(
                    f"staging payload {name} changed or became unsafe"
                ) from error
            raise SnapshotImportApplyError(
                f"cannot exclusively create staging payload {name}: {error}"
            ) from error
        created = os.fstat(dest_fd)
        if not stat.S_ISREG(created.st_mode):
            raise SnapshotImportApplyIntegrityError(
                f"staged payload {name} is not a regular file"
            )
        owned_files[name] = _file_inode(created)
        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = _read_chunk(source_fd, HASH_CHUNK_BYTES)
            except OSError as error:
                raise _io_error("read import payload", error) from error
            if not chunk:
                break
            _write_all(dest_fd, chunk)
            digest.update(chunk)
            total += len(chunk)
        observed = "sha256:" + digest.hexdigest()
        if total != expected_size or observed != expected_revision:
            raise SnapshotImportApplyIntegrityError(
                f"source payload {name} did not match the fresh import plan "
                "while it was copied"
            )
        after_src = os.fstat(source_fd)
        try:
            path_src = os.stat(name, dir_fd=source_dir_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotImportApplyIntegrityError(
                f"source payload {name} changed while it was copied"
            ) from error
        if (
            _complete_file_identity(after_src) != expected_identity
            or stat.S_ISLNK(path_src.st_mode)
            or not stat.S_ISREG(path_src.st_mode)
            or _complete_file_identity(path_src) != expected_identity
            or total != expected_identity[2]
        ):
            raise SnapshotImportApplyIntegrityError(
                f"source payload {name} changed while it was copied"
            )
        _fsync_file(dest_fd)
        dest_info = os.fstat(dest_fd)
        try:
            dest_path = os.stat(name, dir_fd=staging_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotImportApplyIntegrityError(
                f"staged payload {name} changed after it was copied"
            ) from error
        if (
            not stat.S_ISREG(dest_info.st_mode)
            or dest_info.st_nlink != 1
            or dest_info.st_size != expected_size
            or _file_inode(dest_info) != owned_files[name]
            or stat.S_ISLNK(dest_path.st_mode)
            or not stat.S_ISREG(dest_path.st_mode)
            or dest_path.st_nlink != 1
            or _file_inode(dest_path) != owned_files[name]
            or dest_path.st_size != expected_size
        ):
            raise SnapshotImportApplyIntegrityError(
                f"staged payload {name} is not the planned regular file"
            )
    finally:
        if dest_fd is not None:
            os.close(dest_fd)


def _open_staged_payloads(
    staging_fd: int,
    staging_path: Path,
    records: Sequence[Mapping[str, Any]],
    owned_files: Mapping[str, Tuple[int, int]],
) -> Tuple[Dict[str, int], Dict[str, os.stat_result]]:
    payload_fds: Dict[str, int] = {}
    present: Dict[str, os.stat_result] = {}
    try:
        for record in records:
            name = str(record["path"])
            try:
                listed = os.stat(name, dir_fd=staging_fd, follow_symlinks=False)
            except OSError as error:
                raise SnapshotImportApplyIntegrityError(
                    f"staged payload {name} disappeared: {error}"
                ) from error
            if stat.S_ISLNK(listed.st_mode) or not stat.S_ISREG(listed.st_mode):
                raise SnapshotImportApplyIntegrityError(
                    f"staged payload {name} is not a regular file"
                )
            if listed.st_nlink != 1 or _file_inode(listed) != owned_files[name]:
                raise SnapshotImportApplyIntegrityError(
                    f"staged payload {name} inode was replaced"
                )
            flags = os.O_RDONLY | os.O_NOFOLLOW
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            try:
                fd = os.open(name, flags, dir_fd=staging_fd)
            except OSError as error:
                raise SnapshotImportApplyIntegrityError(
                    f"cannot open staged payload {name}: {error}"
                ) from error
            payload_fds[name] = fd
            held = os.fstat(fd)
            if (
                not stat.S_ISREG(held.st_mode)
                or held.st_nlink != 1
                or _file_inode(held) != owned_files[name]
            ):
                raise SnapshotImportApplyIntegrityError(
                    f"staged payload {name} is not the owned regular file"
                )
            present[name] = listed
        return payload_fds, present
    except Exception:
        for fd in payload_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _verify_staged_envelope(
    staging_fd: int,
    staging_path: Path,
    records: Sequence[Mapping[str, Any]],
    snapshot_id: str,
    source_export_revision: str,
    owned_files: Mapping[str, Tuple[int, int]],
    *,
    expect_writer_lock: bool,
) -> None:
    present_names, saw_lock = _list_staging_names(staging_fd)
    planned = [str(item["path"]) for item in records]
    if set(present_names) != set(planned):
        raise SnapshotImportApplyIntegrityError(
            "import staging listing is not the planned payload set"
        )
    if expect_writer_lock:
        if not saw_lock:
            raise SnapshotImportApplyIntegrityError(
                "staging writer lock disappeared during the staging-write interval"
            )
    elif saw_lock:
        raise SnapshotImportApplyIntegrityError(
            "staging writer lock is still present after release"
        )
    payload_fds, present = _open_staged_payloads(
        staging_fd, staging_path, records, owned_files
    )
    try:
        hashed: List[Dict[str, Any]] = []
        for record in records:
            name = str(record["path"])
            revision, size = _hash_held_fd(
                payload_fds[name],
                path=staging_path / name,
                label=name,
            )
            held = os.fstat(payload_fds[name])
            path_info = os.stat(name, dir_fd=staging_fd, follow_symlinks=False)
            if (
                size != record["size_bytes"]
                or revision != record["content_revision"]
                or stat.S_ISLNK(path_info.st_mode)
                or not stat.S_ISREG(path_info.st_mode)
                or not stat.S_ISREG(held.st_mode)
                or held.st_nlink != 1
                or path_info.st_nlink != 1
                or _file_inode(held) != owned_files[name]
                or _file_inode(path_info) != owned_files[name]
            ):
                raise SnapshotImportApplyIntegrityError(
                    f"staged payload {name} did not match the fresh import plan"
                )
            hashed.append(
                {
                    "path": name,
                    "size_bytes": size,
                    "content_revision": revision,
                }
            )
        staged_revision = export_revision_of(
            {
                "schema_version": PLAN_SCHEMA_VERSION,
                "resolved_snapshot": snapshot_id,
                "files": hashed,
            }
        )
        if staged_revision != source_export_revision:
            raise SnapshotImportApplyIntegrityError(
                "staged export revision does not match source_export_revision"
            )
        manifest_bytes = _read_held_bytes(
            payload_fds[MANIFEST_NAME],
            path=staging_path / MANIFEST_NAME,
            label="manifest",
            max_bytes=MAX_MANIFEST_BYTES,
        )
        manifest = _parse_manifest(manifest_bytes, staging_path / MANIFEST_NAME)
        observed_id = _validate_source_envelope(
            staging_path, payload_fds, present, manifest
        )
        if observed_id != snapshot_id:
            raise SnapshotImportApplyIntegrityError(
                "staged manifest id does not equal the planned snapshot id"
            )
    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error
    finally:
        for fd in payload_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass


def _prove_writer_lock_absent(staging_fd: int, staging_path: Path) -> None:
    try:
        os.stat(
            STAGING_WRITER_LOCK_NAME,
            dir_fd=staging_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        pass
    else:
        raise SnapshotImportApplyIntegrityError(
            "staging writer lock still present before promotion: "
            f"{staging_path / STAGING_WRITER_LOCK_NAME}"
        )
    try:
        (staging_path / STAGING_WRITER_LOCK_NAME).lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise SnapshotImportApplyIntegrityError(
            f"cannot prove staging writer lock is absent: {error}"
        ) from error
    raise SnapshotImportApplyIntegrityError(
        "staging writer lock still present before promotion: "
        f"{staging_path / STAGING_WRITER_LOCK_NAME}"
    )


def _cleanup_owned_staging(
    snapshots_fd: int,
    staging_path: Path,
    staging_name: str,
    staging_identity: Tuple[int, int],
    owned_files: Mapping[str, Tuple[int, int]],
    owned_writer_lock: Optional[Tuple[int, int]],
    writer_lock_removed: bool,
) -> None:
    """Remove only this invocation's staging directory and owned children.

    Never follows a replaced pathname and never recursively deletes an
    unresolved tree. Unexpected children are left in place. The final
    rmdir runs only when the pathname, held descriptor, and captured
    creation identity still agree and the directory is empty.
    """
    if not _is_canonical_direct_name(staging_name):
        return
    _before_import_apply_staging_cleanup(snapshots_fd, staging_name)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        staging_fd = os.open(staging_name, flags, dir_fd=snapshots_fd)
    except OSError:
        return
    claim = None
    try:
        opened = os.fstat(staging_fd)
        if _directory_inode(opened) != staging_identity:
            return
        try:
            names, saw_lock = _list_staging_names(
                staging_fd, include_writer_lock=True
            )
        except (SnapshotImportApplyError, OSError):
            return
        if saw_lock:
            if owned_writer_lock is None or writer_lock_removed:
                return
            try:
                claim = acquire_existing_staging_writer_claim(staging_path)
            except StagingWriterLeaseError:
                return
            if claim.inode_identity != owned_writer_lock:
                return
        elif owned_writer_lock is not None and not writer_lock_removed:
            # This invocation observed and held a lock inode, but it vanished
            # without the successful release-and-remove transition.
            return
        for name in names:
            if name == STAGING_WRITER_LOCK_NAME:
                continue
            if name not in owned_files or not _is_canonical_direct_name(name):
                continue
            try:
                info = os.stat(name, dir_fd=staging_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            if _file_inode(info) != owned_files[name]:
                continue
            try:
                os.unlink(name, dir_fd=staging_fd)
            except OSError:
                continue
        try:
            remaining_payloads, remaining_lock = _list_staging_names(
                staging_fd, include_writer_lock=False
            )
        except (SnapshotImportApplyError, OSError):
            return
        if remaining_payloads:
            return
        if remaining_lock:
            if claim is None or claim.inode_identity != owned_writer_lock:
                return
            try:
                claim.release_and_remove()
            except StagingWriterLeaseError:
                return
            claim = None
        _before_import_apply_staging_rmdir(snapshots_fd, staging_name)
        try:
            opened = os.fstat(staging_fd)
            path_identity = os.stat(
                staging_name, dir_fd=snapshots_fd, follow_symlinks=False
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
        except (SnapshotImportApplyError, OSError):
            return
        if remaining or saw_lock:
            return
    finally:
        if claim is not None:
            claim.close()
        os.close(staging_fd)
    try:
        os.rmdir(staging_name, dir_fd=snapshots_fd)
    except OSError:
        return


def _file_records(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
    files = plan.get("files")
    if not isinstance(files, list):
        raise SnapshotImportApplyError("fresh import plan is missing files")
    records: List[Dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise SnapshotImportApplyError("fresh import plan file record is malformed")
        path = item.get("path")
        size = item.get("size_bytes")
        revision = item.get("content_revision")
        if not isinstance(path, str) or not _is_canonical_direct_name(path):
            raise SnapshotImportApplyError(
                f"fresh import plan file path is not a canonical direct name: {path!r}"
            )
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SnapshotImportApplyError(
                f"fresh import plan file size is not a non-negative integer: {size!r}"
            )
        if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
            raise SnapshotImportApplyError(
                "fresh import plan content_revision must be sha256:<64 lowercase hex>"
            )
        records.append(
            {
                "path": path,
                "size_bytes": size,
                "content_revision": revision,
            }
        )
    paths = [item["path"] for item in records]
    if len(set(paths)) != len(paths) or paths != _byte_sort(paths):
        raise SnapshotImportApplyError(
            "fresh import plan files must be unique and sorted in UTF-8-byte order"
        )
    return records


def _capture_target_core(root: Path) -> Dict[str, Any]:
    """Capture target CAS fields without reading unrelated staging payloads."""
    try:
        _require_managed_graph(root)
        lock_identity = _target_lock_identity(root)
        current, current_identity = _read_target_current(root)
        published, _staging_paths = _list_target_snapshot_entries(root)
    except (SnapshotImportPlanError, SnapshotStagingError) as error:
        raise _wrap_plan_error(error) from error
    if current not in set(published):
        raise SnapshotImportApplyIntegrityError(
            "current is not a member of the published snapshot set"
        )
    return {
        "lock_identity": lock_identity,
        "current": current,
        "current_identity": current_identity,
        "published": published,
    }


def _capture_stable_target_core(root: Path) -> Dict[str, Any]:
    first = _capture_target_core(root)
    second = _capture_target_core(root)
    if first != second:
        raise SnapshotImportApplyIntegrityError(
            "publication lock, current, or published snapshots changed "
            "during import apply"
        )
    return second


def _validate_cas_under_exclusive_lease(
    graph_path: Path,
    graph_fd: int,
    graph_inode: Tuple[int, int],
    snapshots_path: Path,
    snapshots_fd: int,
    snapshots_inode: Tuple[int, int],
    staging_name: str,
    staging_fd: int,
    staging_identity: Tuple[int, int],
    snapshot_id: str,
    source: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    source_export_revision: str,
    owned_files: Mapping[str, Tuple[int, int]],
    plan_tokens: Mapping[str, Any],
    staging_path: Path,
) -> None:
    _require_held_directory_inode(
        graph_path, graph_fd, graph_inode, label="graph root"
    )
    _require_held_directory_inode(
        snapshots_path, snapshots_fd, snapshots_inode, label="snapshots directory"
    )
    later = _capture_stable_target_core(graph_path)
    expected = plan_tokens["consistency"]
    if later["lock_identity"] != expected["lock_identity"]:
        raise SnapshotImportApplyIntegrityError(
            "publication lock identity changed during import apply"
        )
    if (
        later["current"] != expected["current"]
        or later["current_identity"] != expected["current_identity"]
    ):
        raise SnapshotImportApplyIntegrityError(
            "current pointer changed during import apply"
        )
    if list(later["published"]) != list(expected["published"]):
        raise SnapshotImportApplyIntegrityError(
            "published snapshot set changed during import apply"
        )
    if snapshot_id in set(later["published"]):
        raise SnapshotImportApplyIntegrityError(
            f"target snapshot id is already published: {snapshot_id}"
        )
    _child_absent(snapshots_fd, snapshot_id, label="published snapshot")
    _require_staging_name_is_held(
        snapshots_fd, staging_name, staging_fd, staging_identity
    )
    _reobserve_held_source(source)
    _verify_staged_envelope(
        staging_fd,
        staging_path,
        records,
        snapshot_id,
        source_export_revision,
        owned_files,
        expect_writer_lock=True,
    )


def _build_result(
    *,
    root: Path,
    export_directory: Path,
    snapshot_id: str,
    records: Sequence[Mapping[str, Any]],
    expected: str,
    observed: str,
    source_export_revision: str,
    current_before: str,
    current_after: Optional[str],
    current_unchanged: bool,
    ok: bool,
    partial: bool,
    import_performed: bool,
    publication_attempted: bool,
    publication_performed: bool,
    snapshot_verified_after_publication: bool,
    staging_created: bool,
    staging_cleanup_attempted: bool,
    staging_remaining: bool,
    snapshots_fsync_confirmed: bool,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "ok": ok,
        "graph": str(root),
        "export_directory": str(export_directory),
        "snapshot_id": snapshot_id,
        "expected_import_revision": expected,
        "observed_import_revision": observed,
        "source_export_revision": source_export_revision,
        "planned_files": [dict(item) for item in records],
        "file_count": len(records),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in records),
        "import_confirmed": True,
        "import_performed": import_performed,
        "publication_attempted": publication_attempted,
        "publication_performed": publication_performed,
        "snapshot_verified_after_publication": snapshot_verified_after_publication,
        "current_before": current_before,
        "current_after": current_after,
        "current_unchanged": current_unchanged,
        "staging_created": staging_created,
        "staging_cleanup_attempted": staging_cleanup_attempted,
        "staging_remaining": staging_remaining,
        "snapshots_fsync_confirmed": snapshots_fsync_confirmed,
        "partial": partial,
        "filesystem_may_have_changed": True,
        "retry_requires_fresh_plan": partial,
        "graph_mutated": True,
        "export_mutated": False,
        "activation_performed": False,
        "retention_performed": False,
        "error": error,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }


def _close_fd(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


@contextmanager
def _snapshot_import_apply_scope(
    graph: Path,
    export_dir: Path,
    expected_import_revision: object,
    *,
    import_confirmed: bool,
) -> Iterator[Dict[str, Any]]:
    """Yield one apply result while source descriptors remain held.

    The exclusive graph lease is held through successful or
    post-publication result serialization. Pre-publication partial
    results keep source and target descriptors open without that
    exclusive lease.
    """
    if not import_confirmed:
        raise SnapshotImportApplyError(_CONFIRMATION_MESSAGE)
    expected = parse_import_revision(expected_import_revision)
    _require_import_apply_primitives()
    try:
        source_path, source_path_identity = _resolve_export_dir(export_dir)
        graph_path, graph_path_identity = _resolve_graph_root(graph)
    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error
    try:
        _require_managed_graph(graph_path)
    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error
    lock_path = graph_path / PUBLICATION_LOCK_NAME
    try:
        lock_info = lock_path.lstat()
    except FileNotFoundError as error:
        raise SnapshotImportApplyError(
            f"publication lock is missing; refusing an import of "
            f"an unleased managed graph: {lock_path}\n{_MISSING_LOCK_HINT}"
        ) from error
    except OSError as error:
        raise SnapshotImportApplyError(
            f"cannot inspect publication lock {lock_path}: {error}"
        ) from error
    if stat.S_ISLNK(lock_info.st_mode) or not stat.S_ISREG(lock_info.st_mode):
        raise SnapshotImportApplyIntegrityError(
            f"publication lock is not a regular file: {lock_path}"
        )
    if not rename_noreplace_supported():
        raise SnapshotImportApplyError(
            "atomic no-replace directory publication is unsupported on "
            f"{sys.platform!r}"
        )

    try:
        with _held_standalone_export_observation(
            source_path, source_path_identity
        ) as source:
            graph_fd: Optional[int] = None
            snapshots_fd: Optional[int] = None
            staging_fd: Optional[int] = None
            staging_name: Optional[str] = None
            staging_path: Optional[Path] = None
            staging_identity: Optional[Tuple[int, int]] = None
            owned_files: Dict[str, Tuple[int, int]] = {}
            owned_writer_lock: Optional[Tuple[int, int]] = None
            writer_lock_removed = False
            published = False
            publication_attempted = False
            staging_created = False
            records: List[Dict[str, Any]] = []
            observed = expected
            source_export_revision = str(source["source_export_revision"])
            snapshot_id = str(source["snapshot_id"])
            current_before = ""
            canonical_graph = graph_path
            canonical_export = Path(source["export_directory"])
            try:
                try:
                    _require_path_identity(
                        graph_path, graph_path_identity, label="graph root"
                    )
                    with graph_read_lease(graph_path, allow_unlocked_managed=False):
                        canonical_graph, graph_fd, graph_identity = _open_anchored_graph(
                            graph_path, graph_path_identity
                        )
                        graph_inode = (graph_identity[0], graph_identity[1])
                        snapshots_path, snapshots_fd, snapshots_inode = (
                            _open_anchored_snapshots(
                                canonical_graph, graph_fd, graph_inode
                            )
                        )
                        try:
                            plan, target = _observe_fresh_import_plan(
                                canonical_graph,
                                graph_fd,
                                graph_identity,
                                source,
                            )
                        except SnapshotImportPlanError as error:
                            raise _wrap_plan_error(error) from error
                        observed = str(plan.get("import_revision") or "")
                        if observed != expected:
                            raise SnapshotImportApplyIntegrityError(
                                "expected import_revision no longer matches the freshly "
                                f"recomputed plan: {observed!r} != {expected!r}"
                            )
                        if plan.get("import_ready") is not True:
                            raise SnapshotImportApplyIntegrityError(
                                "expected import_revision belongs to a blocked import plan"
                            )
                        if (
                            plan.get("target_snapshot_present") is not False
                            or plan.get("target_staging_present") is not False
                        ):
                            raise SnapshotImportApplyIntegrityError(
                                "target snapshot id or exact staging name is already present"
                            )
                        records = _file_records(plan)
                        snapshot_id = str(plan["snapshot_id"])
                        if snapshot_id != source["snapshot_id"]:
                            raise SnapshotImportApplyIntegrityError(
                                "fresh import plan snapshot id does not match the source manifest id"
                            )
                        source_export_revision = str(plan["source_export_revision"])
                        current_before = str(plan["current"])
                        staging_name = str(plan["target_staging_name"])
                        expected_staging = f"{STAGING_NAME_PREFIX}{snapshot_id}"
                        if staging_name != expected_staging:
                            raise SnapshotImportApplyError(
                                "fresh import plan target_staging_name must be exactly "
                                ".staging-<snapshot-id>"
                            )
                        plan_tokens = target["tokens"]
                        _after_import_apply_plan_computed(canonical_graph, plan)
                    _after_import_apply_shared_lease_released(
                        canonical_graph, snapshots_fd
                    )
                    _require_held_directory_inode(
                        canonical_graph, graph_fd, graph_inode, label="graph root"
                    )
                    _require_held_directory_inode(
                        snapshots_path,
                        snapshots_fd,
                        snapshots_inode,
                        label="snapshots directory",
                    )
                    _child_absent(
                        snapshots_fd, snapshot_id, label="published snapshot"
                    )
                    _child_absent(
                        snapshots_fd, staging_name, label="import staging directory"
                    )
                    staging_path = snapshots_path / staging_name
                    _mkdir_exact_staging(snapshots_fd, staging_name)
                    staging_created = True
                    staging_identity = _capture_created_staging_identity(
                        snapshots_fd, staging_name
                    )
                    staging_fd = _open_exact_staging(
                        snapshots_fd, staging_name, staging_identity
                    )
                    try:
                        with staging_writer_lease(staging_path) as writer_lease:
                            owned_writer_lock = writer_lease.inode_identity
                            _require_staging_name_is_held(
                                snapshots_fd,
                                staging_name,
                                staging_fd,
                                staging_identity,
                            )
                            _after_import_apply_writer_lease(
                                snapshots_fd, staging_name, staging_fd
                            )
                            _before_import_apply_copy(canonical_graph, records)
                            source_dir_fd = int(source["directory_fd"])
                            payload_fds = source["payload_fds"]
                            identities = source["payload_identities"]
                            for record in records:
                                name = str(record["path"])
                                _copy_one_payload(
                                    payload_fds[name],
                                    source_dir_fd,
                                    staging_fd,
                                    record,
                                    identities[name],
                                    owned_files,
                                )
                            _fsync_staging_directory(staging_fd)
                            _after_import_apply_copied(canonical_graph, records)
                            _verify_staged_envelope(
                                staging_fd,
                                staging_path,
                                records,
                                snapshot_id,
                                source_export_revision,
                                owned_files,
                                expect_writer_lock=True,
                            )
                            _after_import_apply_staged_verified(
                                canonical_graph, records
                            )
                            _reobserve_held_source(source)
                            _after_import_apply_source_reobserved(
                                canonical_graph, records
                            )
                            _before_import_apply_exclusive_lease(
                                canonical_graph, staging_name
                            )
                            with graph_exclusive_lease(canonical_graph):
                                _after_import_apply_exclusive_lease(
                                    canonical_graph, staging_name
                                )
                                _validate_cas_under_exclusive_lease(
                                    canonical_graph,
                                    graph_fd,
                                    graph_inode,
                                    snapshots_path,
                                    snapshots_fd,
                                    snapshots_inode,
                                    staging_name,
                                    staging_fd,
                                    staging_identity,
                                    snapshot_id,
                                    source,
                                    records,
                                    source_export_revision,
                                    owned_files,
                                    plan_tokens,
                                    staging_path,
                                )
                                _before_import_apply_writer_lock_remove(
                                    snapshots_fd, staging_name, staging_fd
                                )
                                _require_staging_name_is_held(
                                    snapshots_fd,
                                    staging_name,
                                    staging_fd,
                                    staging_identity,
                                )
                                try:
                                    writer_lease.release_and_remove()
                                except StagingWriterLeaseError as error:
                                    raise _wrap_writer_lease_error(error) from error
                                writer_lock_removed = True
                                _prove_writer_lock_absent(staging_fd, staging_path)
                                _after_import_apply_writer_lock_removed(
                                    snapshots_fd, staging_name, staging_fd
                                )
                                _verify_staged_envelope(
                                    staging_fd,
                                    staging_path,
                                    records,
                                    snapshot_id,
                                    source_export_revision,
                                    owned_files,
                                    expect_writer_lock=False,
                                )
                                _fsync_staging_directory(staging_fd)
                                _before_import_apply_publication(
                                    snapshots_fd, snapshot_id, staging_name
                                )
                                _reobserve_held_source(source)
                                _require_held_directory_inode(
                                    snapshots_path,
                                    snapshots_fd,
                                    snapshots_inode,
                                    label="snapshots directory",
                                )
                                _require_staging_name_is_held(
                                    snapshots_fd,
                                    staging_name,
                                    staging_fd,
                                    staging_identity,
                                )
                                try:
                                    publication_attempted = True
                                    _publish_staging(
                                        snapshots_fd, staging_name, snapshot_id
                                    )
                                except RenameNoreplaceError as error:
                                    if error.errno == errno.EEXIST:
                                        raise SnapshotImportApplyIntegrityError(
                                            f"published snapshot already exists: {snapshot_id}"
                                        ) from error
                                    raise SnapshotImportApplyError(
                                        f"atomic snapshot publication failed: {error}"
                                    ) from error
                                published = True
                                snapshot_verified = False
                                fsync_confirmed = False
                                errors: List[str] = []
                                current_after: Optional[str] = None
                                current_unchanged = False
                                staging_remaining_after = False
                                try:
                                    _after_import_apply_published(
                                        snapshots_fd, snapshot_id
                                    )
                                    dest_inode = _path_child_inode(
                                        snapshots_fd,
                                        snapshot_id,
                                        label="published snapshot",
                                    )
                                    held = _directory_inode(os.fstat(staging_fd))
                                    staging_remaining_after = _pathname_present(
                                        snapshots_fd, staging_name
                                    )
                                    published_identity_matches = not (
                                        dest_inode != staging_identity
                                        or held != staging_identity
                                    )
                                    if not published_identity_matches:
                                        errors.append(
                                            "published snapshot is not the held "
                                            "import staging directory"
                                        )
                                    if staging_remaining_after:
                                        errors.append(
                                            "import staging pathname remains after publication"
                                        )
                                    elif published_identity_matches:
                                        published_path = snapshots_path / snapshot_id
                                        try:
                                            _verify_staged_envelope(
                                                staging_fd,
                                                published_path,
                                                records,
                                                snapshot_id,
                                                source_export_revision,
                                                owned_files,
                                                expect_writer_lock=False,
                                            )
                                            snapshot_verified = True
                                            _reobserve_held_source(source)
                                            _require_held_directory_inode(
                                                canonical_graph,
                                                graph_fd,
                                                graph_inode,
                                                label="graph root",
                                            )
                                            _require_held_directory_inode(
                                                snapshots_path,
                                                snapshots_fd,
                                                snapshots_inode,
                                                label="snapshots directory",
                                            )
                                            later_cons = _capture_stable_target_core(
                                                canonical_graph
                                            )
                                            expected_cons = plan_tokens["consistency"]
                                            current_after = str(later_cons["current"])
                                            current_unchanged = bool(
                                                later_cons["current"]
                                                == expected_cons["current"]
                                                and later_cons["current_identity"]
                                                == expected_cons["current_identity"]
                                            )
                                            if not current_unchanged:
                                                errors.append(
                                                    "current pointer changed during publication"
                                                )
                                            expected_published = _byte_sort(
                                                list(expected_cons["published"])
                                                + [snapshot_id]
                                            )
                                            if list(later_cons["published"]) != expected_published:
                                                errors.append(
                                                    "published history is not the "
                                                    "prior set plus the imported id"
                                                )
                                            elif later_cons["lock_identity"] != expected_cons[
                                                "lock_identity"
                                            ]:
                                                errors.append(
                                                    "publication lock identity "
                                                    "changed during publication"
                                                )
                                            _after_import_apply_post_publication_target_observed(
                                                canonical_graph, snapshot_id
                                            )
                                            _reobserve_held_source(source)
                                            _require_held_directory_inode(
                                                canonical_graph,
                                                graph_fd,
                                                graph_inode,
                                                label="graph root",
                                            )
                                            _require_held_directory_inode(
                                                snapshots_path,
                                                snapshots_fd,
                                                snapshots_inode,
                                                label="snapshots directory",
                                            )
                                        except (
                                            SnapshotImportApplyError,
                                            SnapshotImportPlanError,
                                            SnapshotStagingError,
                                            OSError,
                                        ) as error:
                                            errors.append(str(error))
                                except (
                                    SnapshotImportApplyError,
                                    SnapshotImportPlanError,
                                    OSError,
                                ) as error:
                                    errors.append(str(error))
                                try:
                                    _fsync_snapshots_directory(snapshots_fd)
                                    fsync_confirmed = True
                                except SnapshotImportApplyError as error:
                                    errors.append(str(error))
                                if snapshot_verified and not errors and fsync_confirmed:
                                    result = _build_result(
                                        root=canonical_graph,
                                        export_directory=canonical_export,
                                        snapshot_id=snapshot_id,
                                        records=records,
                                        expected=expected,
                                        observed=observed,
                                        source_export_revision=source_export_revision,
                                        current_before=current_before,
                                        current_after=current_after,
                                        current_unchanged=current_unchanged,
                                        ok=True,
                                        partial=False,
                                        import_performed=True,
                                        publication_attempted=True,
                                        publication_performed=True,
                                        snapshot_verified_after_publication=True,
                                        staging_created=True,
                                        staging_cleanup_attempted=False,
                                        staging_remaining=staging_remaining_after,
                                        snapshots_fsync_confirmed=True,
                                    )
                                else:
                                    result = _build_result(
                                        root=canonical_graph,
                                        export_directory=canonical_export,
                                        snapshot_id=snapshot_id,
                                        records=records,
                                        expected=expected,
                                        observed=observed,
                                        source_export_revision=source_export_revision,
                                        current_before=current_before,
                                        current_after=current_after,
                                        current_unchanged=current_unchanged,
                                        ok=False,
                                        partial=True,
                                        import_performed=True,
                                        publication_attempted=True,
                                        publication_performed=True,
                                        snapshot_verified_after_publication=snapshot_verified,
                                        staging_created=True,
                                        staging_cleanup_attempted=False,
                                        staging_remaining=staging_remaining_after,
                                        snapshots_fsync_confirmed=fsync_confirmed,
                                        error=_bound_error(
                                            "; ".join(errors)
                                            or "post-publication verification failed"
                                        ),
                                    )
                                _after_import_apply_result_ready(
                                    canonical_export,
                                    canonical_graph,
                                    int(source["directory_fd"]),
                                    graph_fd,
                                    snapshots_fd,
                                    staging_fd,
                                    source["payload_fds"],
                                    True,
                                    result,
                                )
                                yield result
                                return
                    except StagingWriterLeaseError as error:
                        raise _wrap_writer_lease_error(error) from error
                except Exception as error:
                    if not staging_created:
                        if isinstance(error, SnapshotImportApplyError):
                            raise
                        if isinstance(error, StagingWriterLeaseError):
                            raise _wrap_writer_lease_error(error) from error
                        if isinstance(
                            error,
                            (
                                SnapshotImportPlanError,
                                SnapshotExportPlanError,
                                SnapshotStagingError,
                            ),
                        ):
                            raise _wrap_plan_error(error) from error
                        if isinstance(
                            error,
                            (ByogPublicationLockError, ByogReaderLockError),
                        ):
                            raise _apply_lock_error(error) from error
                        raise
                    wrapped = _wrap_after_staging_error(error)
                    if published:
                        remaining = bool(
                            snapshots_fd is not None
                            and staging_name is not None
                            and _pathname_present(snapshots_fd, staging_name)
                        )
                        result = _build_result(
                            root=canonical_graph,
                            export_directory=canonical_export,
                            snapshot_id=snapshot_id,
                            records=records,
                            expected=expected,
                            observed=observed,
                            source_export_revision=source_export_revision,
                            current_before=current_before,
                            current_after=None,
                            current_unchanged=False,
                            ok=False,
                            partial=True,
                            import_performed=True,
                            publication_attempted=publication_attempted,
                            publication_performed=True,
                            snapshot_verified_after_publication=False,
                            staging_created=True,
                            staging_cleanup_attempted=False,
                            staging_remaining=remaining,
                            snapshots_fsync_confirmed=False,
                            error=_bound_error(wrapped),
                        )
                    else:
                        cleanup_attempted = False
                        remaining = True
                        if (
                            snapshots_fd is not None
                            and staging_name is not None
                            and staging_path is not None
                            and staging_identity is not None
                        ):
                            cleanup_attempted = True
                            try:
                                with graph_exclusive_lease(canonical_graph):
                                    _require_held_directory_inode(
                                        canonical_graph,
                                        graph_fd,
                                        graph_inode,
                                        label="graph root",
                                    )
                                    _require_held_directory_inode(
                                        snapshots_path,
                                        snapshots_fd,
                                        snapshots_inode,
                                        label="snapshots directory",
                                    )
                                    _cleanup_owned_staging(
                                        snapshots_fd,
                                        staging_path,
                                        staging_name,
                                        staging_identity,
                                        owned_files,
                                        owned_writer_lock,
                                        writer_lock_removed,
                                    )
                            except Exception:
                                pass
                            remaining = _pathname_present(snapshots_fd, staging_name)
                        result = _build_result(
                            root=canonical_graph,
                            export_directory=canonical_export,
                            snapshot_id=snapshot_id,
                            records=records,
                            expected=expected,
                            observed=observed,
                            source_export_revision=source_export_revision,
                            current_before=current_before,
                            current_after=None,
                            current_unchanged=False,
                            ok=False,
                            partial=True,
                            import_performed=False,
                            publication_attempted=publication_attempted,
                            publication_performed=False,
                            snapshot_verified_after_publication=False,
                            staging_created=True,
                            staging_cleanup_attempted=cleanup_attempted,
                            staging_remaining=remaining,
                            snapshots_fsync_confirmed=False,
                            error=_bound_error(wrapped),
                        )
                    _after_import_apply_result_ready(
                        canonical_export,
                        canonical_graph,
                        int(source["directory_fd"]),
                        graph_fd if graph_fd is not None else -1,
                        snapshots_fd if snapshots_fd is not None else -1,
                        staging_fd,
                        source["payload_fds"],
                        False,
                        result,
                    )
                    yield result
                    return
            except SnapshotImportPlanError as error:
                raise _wrap_plan_error(error) from error
            except SnapshotStagingError as error:
                raise _wrap_plan_error(error) from error
            except StagingWriterLeaseError as error:
                raise _wrap_writer_lease_error(error) from error
            except ByogPublicationLockError as error:
                raise _apply_lock_error(error) from error
            except ByogReaderLockError as error:
                raise _apply_lock_error(error) from error
            finally:
                _close_fd(staging_fd)
                _close_fd(snapshots_fd)
                _close_fd(graph_fd)

    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error
    except SnapshotExportPlanError as error:
        raise _wrap_plan_error(error) from error


def snapshot_import_apply(
    graph: Path,
    export_dir: Path,
    expected_import_revision: str,
    *,
    import_confirmed: bool,
) -> Dict[str, Any]:
    """Publish one CAS-verified standalone export as a retained snapshot."""
    with _snapshot_import_apply_scope(
        graph,
        export_dir,
        expected_import_revision,
        import_confirmed=import_confirmed,
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Publish one standalone snapshot export as a retained snapshot "
            "in an existing managed BYOG graph. Requires --import-confirmed "
            "and --expected-import-revision. snapshot-import-plan is the "
            "preview; this command has no dry-run. Does not change current, "
            "pins, or retention, overwrite an existing snapshot id, mutate "
            "the export, or claim backup or recoverability. Never creates "
            ".publish.lock, and is not an MCP tool. A crash may leave "
            ".staging-<snapshot-id> and its .staging-writer.lock protocol file."
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
        "--export-dir",
        type=Path,
        required=True,
        help="Standalone export directory, relative to cwd.",
    )
    parser.add_argument(
        "--expected-import-revision",
        required=True,
        help="sha256:<64 lowercase hex> from a fresh snapshot-import-plan",
    )
    parser.add_argument(
        "--import-confirmed",
        action="store_true",
        help=(
            "Required to create the staging directory and publish the "
            "snapshot. The command still refuses to copy if the recomputed "
            "import_revision no longer matches or the plan is blocked."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_import_apply_scope(
            args.graph,
            args.export_dir,
            args.expected_import_revision,
            import_confirmed=bool(args.import_confirmed),
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing the exclusive graph
            # lease and source descriptors so the complete response is
            # handed to the caller under that protected interval.
            sys.stdout.flush()
            if result.get("partial") or not result.get("ok"):
                return 1
    except SnapshotImportApplyError as error:
        print(f"snapshot-import-apply: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-import-apply: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
