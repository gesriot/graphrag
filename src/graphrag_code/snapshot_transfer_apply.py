#!/usr/bin/env python
"""CAS-guarded snapshot transfer apply.

``snapshot-transfer-apply`` copies one retained published snapshot from
a managed BYOG graph into the retained history of a different managed
BYOG graph without creating a standalone export directory. It preserves
the source snapshot id, copies the exact validated envelope bytes into
``.staging-<snapshot-id>``, and atomically publishes that private
staging directory without replacing any existing entry. It does not
change either ``current``, inspect or modify pins, or run retention.
It never mutates the source graph, never creates or adopts either
``.publish.lock``, and is CLI-only.

Successful transfer means only that the language-independent persisted
snapshot envelope and observed bytes were copied and atomically added
to the target retained history. It is not authenticity, provenance,
backup quality, portability, recoverability, restore success, or
semantic equivalence. A later activation remains a separate explicit
``snapshot-activate --activate-confirmed`` operation.

``--expected-transfer-revision`` is a compare-and-swap token. Apply
reproduces the current schema-1 transfer-plan contract from held
descriptors under one mixed-mode pair: a shared existing-lock lease on
the source and an exclusive existing-lock lease on the target,
acquired in the global two-graph order. It copies only when that token
still matches and the plan is transfer-ready with both the final id
and the exact staging name absent. A stale or blocked revision creates
nothing.

The two graph leases stay held through result construction,
serialization, stdout write, and flush. A crash may leave
``.staging-<id>`` and its persistent writer-lock metadata. Existing
staging inventory/cleanup commands are the only later cleanup path.
After native publication succeeds, a later reporting failure never
deletes the published snapshot.

Usage:
    graphrag-code snapshot-transfer-apply --source-graph <managed-root> \\
        --snapshot <id|current> --target-graph <managed-root> \\
        --expected-transfer-revision sha256:<hex> --transfer-confirmed [--json]
    python -m graphrag_code.snapshot_transfer_apply --source-graph <managed-root> \\
        --snapshot <id|current> --target-graph <managed-root> \\
        --expected-transfer-revision sha256:<hex> --transfer-confirmed [--json]
    uv run python scripts/snapshot_transfer_apply.py --source-graph <managed-root> \\
        --snapshot <id|current> --target-graph <managed-root> \\
        --expected-transfer-revision sha256:<hex> --transfer-confirmed [--json]
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
    STAGING_NAME_PREFIX,
    ByogPublicationLockError,
    ByogReaderLockError,
    StagingWriterLeaseError,
    StagingWriterLockContention,
    StagingWriterLockUnsafe,
    _available_lock_backend,
    graph_source_shared_target_exclusive_leases,
    ordered_graph_lease_pair,
    staging_writer_lease,
)
from graphrag_code.snapshot_export_plan import (
    HASH_CHUNK_BYTES,
    SnapshotExportPlanError,
    _is_canonical_direct_name,
    _parse_snapshot,
    _require_descriptor_reads,
)
from graphrag_code.snapshot_import_apply import (
    PAYLOAD_FILE_MODE,
    STAGING_DIR_MODE,
    SnapshotImportApplyError,
    SnapshotImportApplyIntegrityError,
    _capture_created_staging_identity as _import_capture_created_staging_identity,
    _child_absent as _import_child_absent,
    _cleanup_owned_staging as _import_cleanup_owned_staging,
    _directory_inode,
    _file_inode,
    _mkdir_exact_staging as _import_mkdir_exact_staging,
    _open_exact_staging as _import_open_exact_staging,
    _path_child_inode as _import_path_child_inode,
    _pathname_present as _import_pathname_present,
    _prove_writer_lock_absent as _import_prove_writer_lock_absent,
    _publish_staging as _import_publish_staging,
    _require_held_directory_inode as _import_require_held_directory_inode,
    _require_staging_name_is_held as _import_require_staging_name_is_held,
    _verify_staged_envelope as _import_verify_staged_envelope,
)
from graphrag_code.snapshot_import_plan import (
    SnapshotImportPlanError,
    _complete_file_identity,
)
from graphrag_code.snapshot_staging import SnapshotStagingError
from graphrag_code.snapshot_transfer_plan import (
    SnapshotTransferPlanError,
    SnapshotTransferPlanIntegrityError,
    _byte_sort,
    _inode,
    _lock_error as _plan_lock_error,
    _observe_fresh_transfer_plan,
    _open_anchored_directory,
    _open_snapshots_directory,
    _reject_same_graph,
    _require_path_identity,
    _resolve_existing_real_directory,
    _reobserve_held_source,
    _wrap_staging_error,
)


APPLY_SCHEMA_VERSION = 1
_MAX_ERROR_CHARS = 400
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_CONFIRMATION_MESSAGE = """\
refusing to apply snapshot transfer without --transfer-confirmed.

This is an explicit CLI publication of one retained snapshot from one
managed graph into a different managed graph. snapshot-transfer-plan is
the preview; this command has no dry-run. Confirmation is required even
when the planned action is empty or impossible. The copy is not a
backup, archive, restore kit, or authorization to delete anything. It
does not activate current, pin, prune, or run retention.

--expected-transfer-revision is a compare-and-swap guard: one mixed-mode
source-shared / target-exclusive lease pair recomputes a fresh transfer
plan and copies only when that token still matches and the plan is
transfer-ready. A mismatched or blocked revision creates nothing. A
crash may leave .staging-<snapshot-id> and its .staging-writer.lock
protocol file. Existing staging inventory/cleanup commands are the only
later cleanup path.
""".strip()
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "transfer_is_not_backup",
        "kind": "notice",
        "message": (
            "Successful transfer copies the language-independent persisted "
            "snapshot envelope into the target retained history. It is not a "
            "backup, archive, restore kit, authentic replica, or recoverable "
            "image, and it is not authorization to delete anything."
        ),
    },
    {
        "code": "transfer_revision_is_cas_only",
        "kind": "notice",
        "message": (
            "expected_transfer_revision is a compare-and-swap token for the "
            "freshly reproduced transfer plan. It does not prove provenance, "
            "authenticity, portability, or recoverability."
        ),
    },
    {
        "code": "transfer_is_not_activation",
        "kind": "notice",
        "message": (
            "This command leaves both current pointers unchanged. A later "
            "activation remains a separate explicit snapshot-activate "
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
            "lease release_and_remove protocol while the target graph "
            "lease is exclusive, before atomic publication."
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
            "snapshot-transfer-apply is CLI-only and intentionally absent "
            "from the fixed 13-tool MCP set."
        ),
    },
)


class SnapshotTransferApplyError(Exception):
    """Expected transfer-apply refusal. Default exit 2."""

    exit_code = 2


class SnapshotTransferApplyIntegrityError(SnapshotTransferApplyError):
    """Unsafe structure, CAS mismatch, or concurrent change. Exit 1."""

    exit_code = 1


class SnapshotTransferApplyOutputError(SnapshotTransferApplyError):
    """Result output failed after this invocation mutated the target."""

    exit_code = 1


def parse_transfer_revision(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotTransferApplyError(
            "expected-transfer-revision must be sha256:<64 lowercase hex>"
        )
    if value != value.strip():
        raise SnapshotTransferApplyError(
            "expected-transfer-revision must not contain leading or trailing "
            f"whitespace: {value!r}"
        )
    if not _REVISION_RE.fullmatch(value):
        raise SnapshotTransferApplyError(
            "expected-transfer-revision must be sha256:<64 lowercase hex>, "
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
        "snapshot-transfer-apply: "
        f"source_graph={result.get('source_graph')} "
        f"target_graph={result.get('target_graph')} "
        f"requested={result.get('requested_snapshot')} "
        f"snapshot_id={result.get('snapshot_id')} "
        f"files={result.get('file_count')} "
        f"total_size_bytes={result.get('total_size_bytes')} "
        f"expected_transfer_revision={result.get('expected_transfer_revision')} "
        f"observed_transfer_revision={result.get('observed_transfer_revision')} "
        f"ok={str(bool(result.get('ok'))).lower()} "
        f"partial={str(bool(result.get('partial'))).lower()} "
        f"transfer_performed={str(bool(result.get('transfer_performed'))).lower()} "
        f"publication_performed={str(bool(result.get('publication_performed'))).lower()} "
        f"source_current_unchanged={str(bool(result.get('source_current_unchanged'))).lower()} "
        f"target_current_unchanged={str(bool(result.get('target_current_unchanged'))).lower()} "
        f"staging_created={str(bool(result.get('staging_created'))).lower()} "
        f"target_snapshots_fsync_confirmed={str(bool(result.get('target_snapshots_fsync_confirmed'))).lower()}"
    )
    error = result.get("error")
    if isinstance(error, str) and error:
        text += f" error={error}"
    return (
        text
        + " This copy is not a backup, not an activation, and is not "
        "authorization to delete anything."
    )


def _from_import_apply(error: Exception) -> SnapshotTransferApplyError:
    message = str(error)
    if isinstance(error, SnapshotImportApplyIntegrityError):
        return SnapshotTransferApplyIntegrityError(message)
    if isinstance(error, SnapshotImportApplyError):
        wrapped = SnapshotTransferApplyError(message)
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    return SnapshotTransferApplyError(message)


def _wrap_plan_error(error: Exception) -> SnapshotTransferApplyError:
    if isinstance(error, SnapshotTransferPlanIntegrityError):
        return SnapshotTransferApplyIntegrityError(str(error))
    if isinstance(error, SnapshotTransferPlanError):
        wrapped = SnapshotTransferApplyError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    if isinstance(error, SnapshotImportPlanError):
        wrapped = SnapshotTransferApplyError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    if isinstance(error, SnapshotStagingError):
        staged = _wrap_staging_error(error)
        if isinstance(staged, SnapshotTransferPlanIntegrityError):
            return SnapshotTransferApplyIntegrityError(str(staged))
        out = SnapshotTransferApplyError(str(staged))
        out.exit_code = getattr(staged, "exit_code", 2)
        return out
    return SnapshotTransferApplyError(str(error))


def _apply_lock_error(error: Exception) -> SnapshotTransferApplyError:
    wrapped = _plan_lock_error(error)
    if isinstance(wrapped, SnapshotTransferPlanIntegrityError):
        return SnapshotTransferApplyIntegrityError(str(wrapped))
    if "graph root changed" in str(error) or "graph root disappeared" in str(error):
        return SnapshotTransferApplyIntegrityError(str(error))
    if "publication lock is missing" in str(error):
        return SnapshotTransferApplyError(f"{error}\n{_MISSING_LOCK_HINT}")
    out = SnapshotTransferApplyError(str(wrapped))
    out.exit_code = getattr(wrapped, "exit_code", 2)
    return out


def _wrap_writer_lease_error(error: StagingWriterLeaseError) -> SnapshotTransferApplyError:
    if isinstance(error, (StagingWriterLockUnsafe, StagingWriterLockContention)):
        return SnapshotTransferApplyIntegrityError(str(error))
    message = str(error)
    if "unsupported" in message.lower():
        return SnapshotTransferApplyError(message)
    return SnapshotTransferApplyIntegrityError(message)


def _wrap_after_staging_error(error: Exception) -> SnapshotTransferApplyError:
    if isinstance(error, SnapshotTransferApplyError):
        return error
    if isinstance(error, StagingWriterLeaseError):
        return _wrap_writer_lease_error(error)
    if isinstance(error, (ByogPublicationLockError, ByogReaderLockError)):
        return _apply_lock_error(error)
    if isinstance(
        error,
        (
            SnapshotTransferPlanError,
            SnapshotImportPlanError,
            SnapshotStagingError,
            SnapshotImportApplyError,
        ),
    ):
        if isinstance(error, SnapshotImportApplyError):
            return _from_import_apply(error)
        return _wrap_plan_error(error)
    if isinstance(error, OSError):
        return _io_error("snapshot transfer apply", error)
    return SnapshotTransferApplyError(str(error))


def _require_transfer_apply_primitives() -> None:
    try:
        _require_descriptor_reads()
    except SnapshotExportPlanError as error:
        raise SnapshotTransferApplyError(str(error)) from error
    supported = getattr(os, "supports_dir_fd", set())
    if (
        os.mkdir not in supported
        or os.unlink not in supported
        or os.rmdir not in supported
        or os.open not in supported
        or os.stat not in supported
    ):
        raise SnapshotTransferApplyError(
            "safe descriptor-relative exclusive transfer publication is "
            f"unsupported on this platform: {sys.platform!r}"
        )
    if not rename_noreplace_supported():
        raise SnapshotTransferApplyError(
            "atomic no-replace directory publication is unsupported on "
            f"{sys.platform!r}"
        )
    if _available_lock_backend() is None:
        raise SnapshotTransferApplyError(
            "advisory staging-writer lock is unsupported on "
            f"{sys.platform!r}; refusing to guess writer-lease state"
        )


def _read_chunk(fd: int, size: int) -> bytes:
    return os.read(fd, size)


def _write_chunk(fd: int, data: bytes) -> int:
    return os.write(fd, data)


def _fsync(fd: int) -> None:
    os.fsync(fd)


def _io_error(action: str, error: BaseException) -> SnapshotTransferApplyError:
    return SnapshotTransferApplyError(f"{action} failed: {error}")


def _bound_error(message: object) -> str:
    text = " ".join(str(message).split())
    if not text:
        text = "transfer apply failed"
    if len(text) > _MAX_ERROR_CHARS:
        return text[:_MAX_ERROR_CHARS]
    return text


def _fsync_file(fd: int) -> None:
    try:
        _fsync(fd)
    except OSError as error:
        raise _io_error("fsync transfer payload", error) from error


def _fsync_staging_directory(fd: int) -> None:
    try:
        _fsync(fd)
    except OSError as error:
        raise _io_error("fsync transfer staging directory", error) from error


def _fsync_snapshots_directory(fd: int) -> None:
    try:
        _fsync(fd)
    except OSError as error:
        raise _io_error("fsync snapshots directory", error) from error


def _close_fd(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _call_import(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except SnapshotImportApplyError as error:
        raise _from_import_apply(error) from error


def _child_absent(directory_fd: int, name: str, *, label: str) -> None:
    _call_import(_import_child_absent, directory_fd, name, label=label)


def _pathname_present(directory_fd: int, name: str) -> bool:
    return _import_pathname_present(directory_fd, name)


def _require_held_directory_inode(
    path: Path,
    directory_fd: int,
    expected_inode: Tuple[int, int],
    *,
    label: str,
) -> None:
    _call_import(
        _import_require_held_directory_inode,
        path,
        directory_fd,
        expected_inode,
        label=label,
    )


def _require_staging_name_is_held(
    snapshots_fd: int,
    staging_name: str,
    staging_fd: int,
    staging_identity: Tuple[int, int],
) -> None:
    _call_import(
        _import_require_staging_name_is_held,
        snapshots_fd,
        staging_name,
        staging_fd,
        staging_identity,
    )


def _mkdir_exact_staging(snapshots_fd: int, staging_name: str) -> None:
    _call_import(_import_mkdir_exact_staging, snapshots_fd, staging_name)


def _capture_created_staging_identity(
    snapshots_fd: int, staging_name: str
) -> Tuple[int, int]:
    return _call_import(
        _import_capture_created_staging_identity, snapshots_fd, staging_name
    )


def _open_exact_staging(
    snapshots_fd: int, staging_name: str, identity: Tuple[int, int]
) -> int:
    return _call_import(
        _import_open_exact_staging, snapshots_fd, staging_name, identity
    )


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
    _call_import(
        _import_verify_staged_envelope,
        staging_fd,
        staging_path,
        records,
        snapshot_id,
        source_export_revision,
        owned_files,
        expect_writer_lock=expect_writer_lock,
    )


def _prove_writer_lock_absent(staging_fd: int, staging_path: Path) -> None:
    _call_import(_import_prove_writer_lock_absent, staging_fd, staging_path)


def _publish_staging(
    snapshots_fd: int, staging_name: str, snapshot_id: str
) -> None:
    try:
        _import_publish_staging(snapshots_fd, staging_name, snapshot_id)
    except RenameNoreplaceError:
        raise


def _cleanup_owned_staging(
    snapshots_fd: int,
    staging_path: Path,
    staging_name: str,
    staging_identity: Tuple[int, int],
    owned_files: Mapping[str, Tuple[int, int]],
    owned_writer_lock: Optional[Tuple[int, int]],
    writer_lock_removed: bool,
) -> None:
    _before_transfer_apply_staging_cleanup(snapshots_fd, staging_name)
    _import_cleanup_owned_staging(
        snapshots_fd,
        staging_path,
        staging_name,
        staging_identity,
        owned_files,
        owned_writer_lock,
        writer_lock_removed,
    )


def _path_child_inode(
    directory_fd: int, name: str, *, label: str
) -> Tuple[int, int]:
    return _call_import(_import_path_child_inode, directory_fd, name, label=label)


def _after_transfer_source_path_inspected(_source: Path) -> None:
    return None


def _after_transfer_target_path_inspected(_target: Path) -> None:
    return None


def _after_transfer_graphs_identified(_source: Path, _target: Path) -> None:
    return None


def _after_transfer_apply_plan_computed(
    _source: Path, _target: Path, _plan: Mapping[str, Any]
) -> None:
    """Test hook after the fresh plan and CAS, before staging creation."""
    return None


def _after_transfer_apply_staging_mkdir(
    _snapshots_fd: int, _staging_name: str
) -> None:
    return None


def _after_transfer_apply_staging_opened(
    _snapshots_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    return None


def _after_transfer_apply_writer_lease(
    _snapshots_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    return None


def _before_transfer_apply_copy(
    _source: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    return None


def _after_transfer_apply_payload_copied(_name: str) -> None:
    """Test hook after one payload copy, including that payload's last hash."""
    return None


def _after_transfer_apply_copied(
    _source: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    return None


def _after_transfer_apply_staged_verified(
    _source: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    return None


def _after_transfer_apply_source_reobserved(
    _source: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    return None


def _before_transfer_apply_writer_lock_remove(
    _snapshots_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    return None


def _after_transfer_apply_writer_lock_removed(
    _snapshots_fd: int, _staging_name: str, _staging_fd: int
) -> None:
    return None


def _before_transfer_apply_publication(
    _snapshots_fd: int, _snapshot_id: str, _staging_name: str
) -> None:
    return None


def _after_transfer_apply_published(
    _snapshots_fd: int, _snapshot_id: str
) -> None:
    return None


def _after_transfer_apply_post_publication_target_observed(
    _target: Path, _snapshot_id: str
) -> None:
    return None


def _before_transfer_apply_staging_cleanup(
    _snapshots_fd: int, _staging_name: str
) -> None:
    return None


def _after_transfer_apply_result_ready(
    _source: Path,
    _target: Path,
    _source_fd: int,
    _target_fd: int,
    _source_snapshots_fd: int,
    _target_snapshots_fd: int,
    _selected_fd: int,
    _staging_fd: Optional[int],
    _payload_fds: Mapping[str, int],
    _result: Mapping[str, Any],
) -> None:
    return None


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        try:
            written = _write_chunk(fd, data[offset:])
        except OSError as error:
            raise _io_error("write transfer payload", error) from error
        if written <= 0:
            raise SnapshotTransferApplyError(
                "short write while copying transfer payload"
            )
        offset += written


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
        raise SnapshotTransferApplyError(
            f"transfer plan file path is not a canonical direct name: {name!r}"
        )
    expected_size = record["size_bytes"]
    expected_revision = record["content_revision"]
    try:
        os.lseek(source_fd, 0, os.SEEK_SET)
    except OSError as error:
        raise SnapshotTransferApplyIntegrityError(
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
                raise SnapshotTransferApplyError(
                    f"staging payload collision: {name}"
                ) from error
            if error.errno in {errno.ELOOP, errno.ENOENT}:
                raise SnapshotTransferApplyIntegrityError(
                    f"staging payload {name} changed or became unsafe"
                ) from error
            raise SnapshotTransferApplyError(
                f"cannot exclusively create staging payload {name}: {error}"
            ) from error
        created = os.fstat(dest_fd)
        if not stat.S_ISREG(created.st_mode):
            raise SnapshotTransferApplyIntegrityError(
                f"staged payload {name} is not a regular file"
            )
        owned_files[name] = _file_inode(created)
        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = _read_chunk(source_fd, HASH_CHUNK_BYTES)
            except OSError as error:
                raise _io_error("read transfer payload", error) from error
            if not chunk:
                break
            _write_all(dest_fd, chunk)
            digest.update(chunk)
            total += len(chunk)
        observed = "sha256:" + digest.hexdigest()
        if total != expected_size or observed != expected_revision:
            raise SnapshotTransferApplyIntegrityError(
                f"source payload {name} did not match the fresh transfer plan "
                "while it was copied"
            )
        after_src = os.fstat(source_fd)
        try:
            path_src = os.stat(name, dir_fd=source_dir_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotTransferApplyIntegrityError(
                f"source payload {name} changed while it was copied"
            ) from error
        if (
            _complete_file_identity(after_src) != expected_identity
            or stat.S_ISLNK(path_src.st_mode)
            or not stat.S_ISREG(path_src.st_mode)
            or _complete_file_identity(path_src) != expected_identity
            or total != expected_identity[2]
        ):
            raise SnapshotTransferApplyIntegrityError(
                f"source payload {name} changed while it was copied"
            )
        _fsync_file(dest_fd)
        dest_info = os.fstat(dest_fd)
        try:
            dest_path = os.stat(name, dir_fd=staging_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotTransferApplyIntegrityError(
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
            raise SnapshotTransferApplyIntegrityError(
                f"staged payload {name} is not the planned regular file"
            )
    finally:
        if dest_fd is not None:
            os.close(dest_fd)
    _after_transfer_apply_payload_copied(name)


def _file_records(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
    files = plan.get("files")
    if not isinstance(files, list):
        raise SnapshotTransferApplyError("fresh transfer plan is missing files")
    records: List[Dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise SnapshotTransferApplyError(
                "fresh transfer plan file record is malformed"
            )
        path = item.get("path")
        size = item.get("size_bytes")
        revision = item.get("content_revision")
        if not isinstance(path, str) or not _is_canonical_direct_name(path):
            raise SnapshotTransferApplyError(
                f"fresh transfer plan file path is not a canonical direct name: {path!r}"
            )
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SnapshotTransferApplyError(
                f"fresh transfer plan file size is not a non-negative integer: {size!r}"
            )
        if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
            raise SnapshotTransferApplyError(
                "fresh transfer plan content_revision must be sha256:<64 lowercase hex>"
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
        raise SnapshotTransferApplyError(
            "fresh transfer plan files must be unique and sorted in UTF-8-byte order"
        )
    return records


def _build_result(
    *,
    source_graph: Path,
    target_graph: Path,
    requested: str,
    snapshot_id: str,
    records: Sequence[Mapping[str, Any]],
    expected: str,
    observed: str,
    source_export_revision: str,
    source_current_before: str,
    source_current_after: Optional[str],
    source_current_unchanged: bool,
    target_current_before: str,
    target_current_after: Optional[str],
    target_current_unchanged: bool,
    ok: bool,
    partial: bool,
    transfer_performed: bool,
    publication_attempted: bool,
    publication_performed: bool,
    snapshot_verified_after_publication: bool,
    staging_created: bool,
    staging_cleanup_attempted: bool,
    staging_remaining: bool,
    target_snapshots_fsync_confirmed: bool,
    target_graph_mutated: bool,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "ok": ok,
        "partial": partial,
        "source_graph": str(source_graph),
        "target_graph": str(target_graph),
        "requested_snapshot": requested,
        "snapshot_id": snapshot_id,
        "expected_transfer_revision": expected,
        "observed_transfer_revision": observed,
        "source_export_revision": source_export_revision,
        "planned_files": [dict(item) for item in records],
        "file_count": len(records),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in records),
        "transfer_confirmed": True,
        "transfer_performed": transfer_performed,
        "publication_attempted": publication_attempted,
        "publication_performed": publication_performed,
        "snapshot_verified_after_publication": snapshot_verified_after_publication,
        "source_graph_mutated": False,
        "target_graph_mutated": target_graph_mutated,
        "source_current_before": source_current_before,
        "source_current_after": source_current_after,
        "source_current_unchanged": source_current_unchanged,
        "target_current_before": target_current_before,
        "target_current_after": target_current_after,
        "target_current_unchanged": target_current_unchanged,
        "staging_created": staging_created,
        "staging_cleanup_attempted": staging_cleanup_attempted,
        "staging_remaining": staging_remaining,
        "target_snapshots_fsync_confirmed": target_snapshots_fsync_confirmed,
        "activation_performed": False,
        "retention_performed": False,
        "filesystem_may_have_changed": bool(staging_created or publication_performed),
        "retry_requires_fresh_plan": True,
        "error": error,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }


def _close_source_observation(source: Optional[Mapping[str, Any]]) -> None:
    if source is None:
        return
    for fd in source.get("payload_fds", {}).values():
        _close_fd(int(fd))
    directory_fd = source.get("directory_fd")
    if directory_fd is not None:
        _close_fd(int(directory_fd))


@contextmanager
def _snapshot_transfer_apply_scope(
    source_graph: Path,
    snapshot: object,
    target_graph: Path,
    expected_transfer_revision: object,
    *,
    transfer_confirmed: bool,
) -> Iterator[Dict[str, Any]]:
    """Yield one apply result while mixed-mode leases and descriptors remain held."""
    if not transfer_confirmed:
        raise SnapshotTransferApplyError(_CONFIRMATION_MESSAGE)
    expected = parse_transfer_revision(expected_transfer_revision)
    _require_transfer_apply_primitives()
    try:
        requested = _parse_snapshot(snapshot)
    except SnapshotExportPlanError as error:
        wrapped = SnapshotTransferApplyError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        raise wrapped from error
    try:
        source_path, source_path_identity = _resolve_existing_real_directory(
            source_graph, label="source graph root"
        )
        _after_transfer_source_path_inspected(source_path)
        target_path, target_path_identity = _resolve_existing_real_directory(
            target_graph, label="target graph root"
        )
        _after_transfer_target_path_inspected(target_path)
        _reject_same_graph(
            source_path, source_path_identity, target_path, target_path_identity
        )
    except SnapshotTransferPlanError as error:
        raise _wrap_plan_error(error) from error
    _after_transfer_graphs_identified(source_path, target_path)
    try:
        _require_path_identity(
            source_path, source_path_identity, label="source graph root"
        )
        _require_path_identity(
            target_path, target_path_identity, label="target graph root"
        )
        source_path, source_fd, source_identity, _source_opened = (
            _open_anchored_directory(
                source_path, source_path_identity, label="source graph root"
            )
        )
    except SnapshotTransferPlanError as error:
        raise _wrap_plan_error(error) from error
    source_obs: Optional[Dict[str, Any]] = None
    target_snapshots_fd: Optional[int] = None
    source_snapshots_fd: Optional[int] = None
    staging_fd: Optional[int] = None
    try:
        try:
            target_path, target_fd, target_identity, _target_opened = (
                _open_anchored_directory(
                    target_path, target_path_identity, label="target graph root"
                )
            )
        except SnapshotTransferPlanError as error:
            raise _wrap_plan_error(error) from error
        try:
            _reject_same_graph(
                source_path, source_identity, target_path, target_identity
            )
            first_root, second_root = ordered_graph_lease_pair(
                source_path,
                _inode(source_identity),
                target_path,
                _inode(target_identity),
            )
            _ = (first_root, second_root)
            try:
                with graph_source_shared_target_exclusive_leases(
                    source_path, target_path
                ):
                    (
                        source_snapshots_path,
                        source_snapshots_fd,
                        source_snapshots_identity,
                        _source_snapshots_opened,
                    ) = _open_snapshots_directory(
                        source_path, source_fd, source_identity
                    )
                    try:
                        (
                            target_snapshots_path,
                            target_snapshots_fd,
                            target_snapshots_identity,
                            _target_snapshots_opened,
                        ) = _open_snapshots_directory(
                            target_path, target_fd, target_identity
                        )
                        try:
                            yield from _run_transfer_under_leases(
                                requested=requested,
                                expected=expected,
                                source_path=source_path,
                                source_fd=source_fd,
                                source_identity=source_identity,
                                source_snapshots_path=source_snapshots_path,
                                source_snapshots_fd=source_snapshots_fd,
                                source_snapshots_identity=source_snapshots_identity,
                                target_path=target_path,
                                target_fd=target_fd,
                                target_identity=target_identity,
                                target_snapshots_path=target_snapshots_path,
                                target_snapshots_fd=target_snapshots_fd,
                                target_snapshots_identity=target_snapshots_identity,
                            )
                        finally:
                            _close_fd(target_snapshots_fd)
                            target_snapshots_fd = None
                    finally:
                        _close_fd(source_snapshots_fd)
                        source_snapshots_fd = None
            except SnapshotTransferApplyError:
                raise
            except SnapshotTransferPlanError as error:
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
            os.close(target_fd)
    finally:
        os.close(source_fd)


def _run_transfer_under_leases(
    *,
    requested: str,
    expected: str,
    source_path: Path,
    source_fd: int,
    source_identity: Tuple[int, int, int, int, int],
    source_snapshots_path: Path,
    source_snapshots_fd: int,
    source_snapshots_identity: Tuple[int, int, int, int, int],
    target_path: Path,
    target_fd: int,
    target_identity: Tuple[int, int, int, int, int],
    target_snapshots_path: Path,
    target_snapshots_fd: int,
    target_snapshots_identity: Tuple[int, int, int, int, int],
) -> Iterator[Dict[str, Any]]:
    source_inode = _inode(source_identity)
    target_inode = _inode(target_identity)
    source_snapshots_inode = _inode(source_snapshots_identity)
    target_snapshots_inode = _inode(target_snapshots_identity)
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
    source_export_revision = ""
    snapshot_id = ""
    source_current_before = ""
    target_current_before = ""
    source_obs: Optional[Dict[str, Any]] = None
    target_obs: Optional[Dict[str, Any]] = None
    result_was_yielded = False
    try:
        try:
            plan, source_obs, target_obs = _observe_fresh_transfer_plan(
                source_path,
                source_fd,
                source_identity,
                source_snapshots_path,
                source_snapshots_fd,
                source_snapshots_identity,
                target_path,
                target_fd,
                target_identity,
                target_snapshots_path,
                target_snapshots_fd,
                target_snapshots_identity,
                requested,
            )
        except SnapshotTransferPlanError as error:
            raise _wrap_plan_error(error) from error
        observed = str(plan.get("transfer_revision") or "")
        if observed != expected:
            raise SnapshotTransferApplyIntegrityError(
                "expected transfer_revision no longer matches the freshly "
                f"recomputed plan: {observed!r} != {expected!r}"
            )
        if plan.get("transfer_ready") is not True:
            raise SnapshotTransferApplyIntegrityError(
                "expected transfer_revision belongs to a blocked transfer plan"
            )
        if plan.get("blocking_reasons") != []:
            raise SnapshotTransferApplyIntegrityError(
                "expected transfer_revision belongs to a blocked transfer plan"
            )
        if plan.get("source_envelope_valid") is not True:
            raise SnapshotTransferApplyIntegrityError(
                "fresh transfer plan source envelope is not valid"
            )
        if (
            plan.get("target_snapshot_present") is not False
            or plan.get("target_staging_present") is not False
        ):
            raise SnapshotTransferApplyIntegrityError(
                "target snapshot id or exact staging name is already present"
            )
        records = _file_records(plan)
        snapshot_id = str(plan["snapshot_id"])
        if snapshot_id != source_obs["snapshot_id"]:
            raise SnapshotTransferApplyIntegrityError(
                "fresh transfer plan snapshot id does not match the source snapshot"
            )
        source_export_revision = str(plan["source_export_revision"])
        source_current_before = str(plan["source_current"])
        target_current_before = str(plan["target_current"])
        staging_name = str(plan["target_staging_name"])
        expected_staging = f"{STAGING_NAME_PREFIX}{snapshot_id}"
        if staging_name != expected_staging:
            raise SnapshotTransferApplyError(
                "fresh transfer plan target_staging_name must be exactly "
                ".staging-<snapshot-id>"
            )
        _after_transfer_apply_plan_computed(source_path, target_path, plan)
        _require_held_directory_inode(
            target_path, target_fd, target_inode, label="target graph root"
        )
        _require_held_directory_inode(
            target_snapshots_path,
            target_snapshots_fd,
            target_snapshots_inode,
            label="snapshots directory",
        )
        _child_absent(target_snapshots_fd, snapshot_id, label="published snapshot")
        _child_absent(
            target_snapshots_fd, staging_name, label="transfer staging directory"
        )
        staging_path = target_snapshots_path / staging_name
        _mkdir_exact_staging(target_snapshots_fd, staging_name)
        staging_created = True
        staging_identity = _capture_created_staging_identity(
            target_snapshots_fd, staging_name
        )
        _after_transfer_apply_staging_mkdir(target_snapshots_fd, staging_name)
        staging_fd = _open_exact_staging(
            target_snapshots_fd, staging_name, staging_identity
        )
        _after_transfer_apply_staging_opened(
            target_snapshots_fd, staging_name, staging_fd
        )
        with staging_writer_lease(staging_path) as writer_lease:
            owned_writer_lock = writer_lease.inode_identity
            _require_staging_name_is_held(
                target_snapshots_fd,
                staging_name,
                staging_fd,
                staging_identity,
            )
            _after_transfer_apply_writer_lease(
                target_snapshots_fd, staging_name, staging_fd
            )
            _before_transfer_apply_copy(source_path, records)
            payload_fds = source_obs["payload_fds"]
            identities = source_obs["payload_identities"]
            source_dir_fd = int(source_obs["directory_fd"])
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
            _after_transfer_apply_copied(source_path, records)
            _verify_staged_envelope(
                staging_fd,
                staging_path,
                records,
                snapshot_id,
                source_export_revision,
                owned_files,
                expect_writer_lock=True,
            )
            _after_transfer_apply_staged_verified(source_path, records)
            _reobserve_held_source(source_obs)
            _after_transfer_apply_source_reobserved(source_path, records)
            later_plan, _ignored_source, later_target = _recheck_cas(
                source_path,
                source_fd,
                source_identity,
                source_snapshots_path,
                source_snapshots_fd,
                source_snapshots_identity,
                target_path,
                target_fd,
                target_identity,
                target_snapshots_path,
                target_snapshots_fd,
                target_snapshots_identity,
                source_obs,
                target_obs,
                snapshot_id,
                expected,
            )
            _ = later_plan
            _before_transfer_apply_writer_lock_remove(
                target_snapshots_fd, staging_name, staging_fd
            )
            _require_staging_name_is_held(
                target_snapshots_fd,
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
            _after_transfer_apply_writer_lock_removed(
                target_snapshots_fd, staging_name, staging_fd
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
            _before_transfer_apply_publication(
                target_snapshots_fd, snapshot_id, staging_name
            )
            _reobserve_held_source(source_obs)
            _require_held_directory_inode(
                target_snapshots_path,
                target_snapshots_fd,
                target_snapshots_inode,
                label="snapshots directory",
            )
            _require_staging_name_is_held(
                target_snapshots_fd,
                staging_name,
                staging_fd,
                staging_identity,
            )
            if later_target["target_snapshot_present"] or snapshot_id in set(
                later_target["published_snapshots"]
            ):
                raise SnapshotTransferApplyIntegrityError(
                    f"target snapshot id is already published: {snapshot_id}"
                )
            try:
                publication_attempted = True
                _publish_staging(target_snapshots_fd, staging_name, snapshot_id)
            except RenameNoreplaceError as error:
                if error.errno == errno.EEXIST:
                    raise SnapshotTransferApplyIntegrityError(
                        f"published snapshot already exists: {snapshot_id}"
                    ) from error
                raise SnapshotTransferApplyError(
                    f"atomic snapshot publication failed: {error}"
                ) from error
            published = True
            result = _finish_published(
                source_path=source_path,
                source_fd=source_fd,
                source_identity=source_identity,
                source_snapshots_path=source_snapshots_path,
                source_snapshots_fd=source_snapshots_fd,
                source_snapshots_identity=source_snapshots_identity,
                target_path=target_path,
                target_fd=target_fd,
                target_identity=target_identity,
                target_snapshots_path=target_snapshots_path,
                target_snapshots_fd=target_snapshots_fd,
                target_snapshots_identity=target_snapshots_identity,
                source_obs=source_obs,
                target_obs=later_target,
                requested=requested,
                snapshot_id=snapshot_id,
                records=records,
                expected=expected,
                observed=observed,
                source_export_revision=source_export_revision,
                source_current_before=source_current_before,
                target_current_before=target_current_before,
                staging_fd=staging_fd,
                staging_name=staging_name,
                staging_identity=staging_identity,
                owned_files=owned_files,
            )
            _after_transfer_apply_result_ready(
                source_path,
                target_path,
                source_fd,
                target_fd,
                source_snapshots_fd,
                target_snapshots_fd,
                int(source_obs["directory_fd"]),
                staging_fd,
                source_obs["payload_fds"],
                result,
            )
            result_was_yielded = True
            yield result
            return
    except Exception as error:
        if result_was_yielded:
            raise SnapshotTransferApplyOutputError(
                "result serialization, stdout write, or flush failed after "
                f"target mutation: {_bound_error(error)}"
            ) from error
        if not staging_created:
            if isinstance(error, SnapshotTransferApplyError):
                raise
            if isinstance(error, StagingWriterLeaseError):
                raise _wrap_writer_lease_error(error) from error
            if isinstance(
                error,
                (
                    SnapshotTransferPlanError,
                    SnapshotImportPlanError,
                    SnapshotStagingError,
                    SnapshotImportApplyError,
                ),
            ):
                if isinstance(error, SnapshotImportApplyError):
                    raise _from_import_apply(error) from error
                raise _wrap_plan_error(error) from error
            if isinstance(error, (ByogPublicationLockError, ByogReaderLockError)):
                raise _apply_lock_error(error) from error
            if isinstance(error, OSError):
                raise _io_error("snapshot transfer apply", error) from error
            raise
        wrapped = _wrap_after_staging_error(error)
        if published:
            remaining = bool(
                staging_name is not None
                and _pathname_present(target_snapshots_fd, staging_name)
            )
            result = _build_result(
                source_graph=source_path,
                target_graph=target_path,
                requested=requested,
                snapshot_id=snapshot_id,
                records=records,
                expected=expected,
                observed=observed,
                source_export_revision=source_export_revision,
                source_current_before=source_current_before,
                source_current_after=None,
                source_current_unchanged=False,
                target_current_before=target_current_before,
                target_current_after=None,
                target_current_unchanged=False,
                ok=False,
                partial=True,
                transfer_performed=True,
                publication_attempted=publication_attempted,
                publication_performed=True,
                snapshot_verified_after_publication=False,
                staging_created=True,
                staging_cleanup_attempted=False,
                staging_remaining=remaining,
                target_snapshots_fsync_confirmed=False,
                target_graph_mutated=True,
                error=_bound_error(wrapped),
            )
        else:
            cleanup_attempted = False
            remaining = True
            if staging_name is not None and staging_path is not None and staging_identity is not None:
                cleanup_attempted = True
                try:
                    _require_held_directory_inode(
                        target_path, target_fd, target_inode, label="target graph root"
                    )
                    _require_held_directory_inode(
                        target_snapshots_path,
                        target_snapshots_fd,
                        target_snapshots_inode,
                        label="snapshots directory",
                    )
                    _cleanup_owned_staging(
                        target_snapshots_fd,
                        staging_path,
                        staging_name,
                        staging_identity,
                        owned_files,
                        owned_writer_lock,
                        writer_lock_removed,
                    )
                except Exception:
                    pass
                remaining = _pathname_present(target_snapshots_fd, staging_name)
            result = _build_result(
                source_graph=source_path,
                target_graph=target_path,
                requested=requested,
                snapshot_id=snapshot_id,
                records=records,
                expected=expected,
                observed=observed,
                source_export_revision=source_export_revision,
                source_current_before=source_current_before,
                source_current_after=None,
                source_current_unchanged=False,
                target_current_before=target_current_before,
                target_current_after=None,
                target_current_unchanged=False,
                ok=False,
                partial=True,
                transfer_performed=False,
                publication_attempted=publication_attempted,
                publication_performed=False,
                snapshot_verified_after_publication=False,
                staging_created=True,
                staging_cleanup_attempted=cleanup_attempted,
                staging_remaining=remaining,
                target_snapshots_fsync_confirmed=False,
                target_graph_mutated=True,
                error=_bound_error(wrapped),
            )
        _after_transfer_apply_result_ready(
            source_path,
            target_path,
            source_fd,
            target_fd,
            source_snapshots_fd,
            target_snapshots_fd,
            int(source_obs["directory_fd"]) if source_obs is not None else -1,
            staging_fd,
            source_obs["payload_fds"] if source_obs is not None else {},
            result,
        )
        try:
            yield result
        except Exception as output_error:
            raise SnapshotTransferApplyOutputError(
                "partial result serialization, stdout write, or flush failed "
                f"after target mutation: {_bound_error(output_error)}"
            ) from output_error
        return
    finally:
        _close_fd(staging_fd)
        _close_source_observation(source_obs)


def _recheck_cas(
    source_path: Path,
    source_fd: int,
    source_identity: Tuple[int, int, int, int, int],
    source_snapshots_path: Path,
    source_snapshots_fd: int,
    source_snapshots_identity: Tuple[int, int, int, int, int],
    target_path: Path,
    target_fd: int,
    target_identity: Tuple[int, int, int, int, int],
    target_snapshots_path: Path,
    target_snapshots_fd: int,
    target_snapshots_identity: Tuple[int, int, int, int, int],
    source_obs: Mapping[str, Any],
    target_obs: Mapping[str, Any],
    snapshot_id: str,
    expected: str,
) -> Tuple[Dict[str, Any], Mapping[str, Any], Dict[str, Any]]:
    from graphrag_code.snapshot_transfer_plan import _capture_target_tokens

    _require_held_directory_inode(
        source_path, source_fd, _inode(source_identity), label="source graph root"
    )
    _require_held_directory_inode(
        source_snapshots_path,
        source_snapshots_fd,
        _inode(source_snapshots_identity),
        label="snapshots directory",
    )
    _require_held_directory_inode(
        target_path, target_fd, _inode(target_identity), label="target graph root"
    )
    _require_held_directory_inode(
        target_snapshots_path,
        target_snapshots_fd,
        _inode(target_snapshots_identity),
        label="snapshots directory",
    )
    _reobserve_held_source(source_obs)
    later = _capture_target_tokens(target_path)
    expected_cons = target_obs["tokens"]["consistency"]
    later_cons = later["consistency"]
    if later_cons["lock_identity"] != expected_cons["lock_identity"]:
        raise SnapshotTransferApplyIntegrityError(
            "publication lock identity changed during transfer apply"
        )
    if (
        later_cons["current"] != expected_cons["current"]
        or later_cons["current_identity"] != expected_cons["current_identity"]
    ):
        raise SnapshotTransferApplyIntegrityError(
            "current pointer changed during transfer apply"
        )
    if list(later_cons["published"]) != list(expected_cons["published"]):
        raise SnapshotTransferApplyIntegrityError(
            "published snapshot set changed during transfer apply"
        )
    if snapshot_id in set(later_cons["published"]):
        raise SnapshotTransferApplyIntegrityError(
            f"target snapshot id is already published: {snapshot_id}"
        )
    _child_absent(target_snapshots_fd, snapshot_id, label="published snapshot")
    return {}, source_obs, target_obs


def _finish_published(
    *,
    source_path: Path,
    source_fd: int,
    source_identity: Tuple[int, int, int, int, int],
    source_snapshots_path: Path,
    source_snapshots_fd: int,
    source_snapshots_identity: Tuple[int, int, int, int, int],
    target_path: Path,
    target_fd: int,
    target_identity: Tuple[int, int, int, int, int],
    target_snapshots_path: Path,
    target_snapshots_fd: int,
    target_snapshots_identity: Tuple[int, int, int, int, int],
    source_obs: Mapping[str, Any],
    target_obs: Mapping[str, Any],
    requested: str,
    snapshot_id: str,
    records: Sequence[Mapping[str, Any]],
    expected: str,
    observed: str,
    source_export_revision: str,
    source_current_before: str,
    target_current_before: str,
    staging_fd: int,
    staging_name: str,
    staging_identity: Tuple[int, int],
    owned_files: Mapping[str, Tuple[int, int]],
) -> Dict[str, Any]:
    from graphrag_code.snapshot_transfer_plan import _capture_target_tokens

    snapshot_verified = False
    fsync_confirmed = False
    errors: List[str] = []
    source_current_after: Optional[str] = None
    source_current_unchanged = False
    target_current_after: Optional[str] = None
    target_current_unchanged = False
    staging_remaining_after = False
    try:
        _after_transfer_apply_published(target_snapshots_fd, snapshot_id)
        dest_inode = _path_child_inode(
            target_snapshots_fd, snapshot_id, label="published snapshot"
        )
        held = _directory_inode(os.fstat(staging_fd))
        staging_remaining_after = _pathname_present(target_snapshots_fd, staging_name)
        published_identity_matches = not (
            dest_inode != staging_identity or held != staging_identity
        )
        if not published_identity_matches:
            errors.append(
                "published snapshot is not the held transfer staging directory"
            )
        if staging_remaining_after:
            errors.append("transfer staging pathname remains after publication")
        elif published_identity_matches:
            published_path = target_snapshots_path / snapshot_id
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
                _reobserve_held_source(source_obs)
                _require_held_directory_inode(
                    source_path,
                    source_fd,
                    _inode(source_identity),
                    label="source graph root",
                )
                _require_held_directory_inode(
                    target_path,
                    target_fd,
                    _inode(target_identity),
                    label="target graph root",
                )
                _require_held_directory_inode(
                    target_snapshots_path,
                    target_snapshots_fd,
                    _inode(target_snapshots_identity),
                    label="snapshots directory",
                )
                later = _capture_target_tokens(target_path)
                inventory = later["inventory"]
                target_current_after = str(inventory.get("current") or "")
                expected_cons = target_obs["tokens"]["consistency"]
                target_current_unchanged = bool(
                    later["consistency"]["current"] == expected_cons["current"]
                    and later["consistency"]["current_identity"]
                    == expected_cons["current_identity"]
                )
                if not target_current_unchanged:
                    errors.append("target current pointer changed during publication")
                expected_published = _byte_sort(
                    list(expected_cons["published"]) + [snapshot_id]
                )
                if list(later["consistency"]["published"]) != expected_published:
                    errors.append(
                        "published history is not the prior set plus the transferred id"
                    )
                elif later["consistency"]["lock_identity"] != expected_cons[
                    "lock_identity"
                ]:
                    errors.append(
                        "publication lock identity changed during publication"
                    )
                source_current_after = str(source_obs["current"])
                source_current_unchanged = bool(
                    source_current_after == source_current_before
                )
                _after_transfer_apply_post_publication_target_observed(
                    target_path, snapshot_id
                )
                _reobserve_held_source(source_obs)
                _require_held_directory_inode(
                    source_path,
                    source_fd,
                    _inode(source_identity),
                    label="source graph root",
                )
                _require_held_directory_inode(
                    source_snapshots_path,
                    source_snapshots_fd,
                    _inode(source_snapshots_identity),
                    label="snapshots directory",
                )
                _require_held_directory_inode(
                    target_path,
                    target_fd,
                    _inode(target_identity),
                    label="target graph root",
                )
                _require_held_directory_inode(
                    target_snapshots_path,
                    target_snapshots_fd,
                    _inode(target_snapshots_identity),
                    label="snapshots directory",
                )
            except (
                SnapshotTransferApplyError,
                SnapshotTransferPlanError,
                SnapshotImportPlanError,
                SnapshotStagingError,
                OSError,
            ) as error:
                errors.append(str(error))
    except (
        SnapshotTransferApplyError,
        SnapshotTransferPlanError,
        OSError,
    ) as error:
        errors.append(str(error))
    try:
        _fsync_snapshots_directory(target_snapshots_fd)
        fsync_confirmed = True
    except SnapshotTransferApplyError as error:
        errors.append(str(error))
    if snapshot_verified and not errors and fsync_confirmed:
        return _build_result(
            source_graph=source_path,
            target_graph=target_path,
            requested=requested,
            snapshot_id=snapshot_id,
            records=records,
            expected=expected,
            observed=observed,
            source_export_revision=source_export_revision,
            source_current_before=source_current_before,
            source_current_after=source_current_after,
            source_current_unchanged=source_current_unchanged,
            target_current_before=target_current_before,
            target_current_after=target_current_after,
            target_current_unchanged=target_current_unchanged,
            ok=True,
            partial=False,
            transfer_performed=True,
            publication_attempted=True,
            publication_performed=True,
            snapshot_verified_after_publication=True,
            staging_created=True,
            staging_cleanup_attempted=False,
            staging_remaining=staging_remaining_after,
            target_snapshots_fsync_confirmed=True,
            target_graph_mutated=True,
        )
    return _build_result(
        source_graph=source_path,
        target_graph=target_path,
        requested=requested,
        snapshot_id=snapshot_id,
        records=records,
        expected=expected,
        observed=observed,
        source_export_revision=source_export_revision,
        source_current_before=source_current_before,
        source_current_after=source_current_after,
        source_current_unchanged=source_current_unchanged,
        target_current_before=target_current_before,
        target_current_after=target_current_after,
        target_current_unchanged=target_current_unchanged,
        ok=False,
        partial=True,
        transfer_performed=True,
        publication_attempted=True,
        publication_performed=True,
        snapshot_verified_after_publication=snapshot_verified,
        staging_created=True,
        staging_cleanup_attempted=False,
        staging_remaining=staging_remaining_after,
        target_snapshots_fsync_confirmed=fsync_confirmed,
        target_graph_mutated=True,
        error=_bound_error(
            "; ".join(errors) or "post-publication verification failed"
        ),
    )


def snapshot_transfer_apply(
    source_graph: Path,
    snapshot: str,
    target_graph: Path,
    expected_transfer_revision: str,
    *,
    transfer_confirmed: bool,
) -> Dict[str, Any]:
    """Publish one CAS-verified retained snapshot into a different managed graph."""
    with _snapshot_transfer_apply_scope(
        source_graph,
        snapshot,
        target_graph,
        expected_transfer_revision,
        transfer_confirmed=transfer_confirmed,
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Publish one retained snapshot from a managed BYOG graph into a "
            "different managed BYOG graph. Requires --transfer-confirmed "
            "and --expected-transfer-revision. snapshot-transfer-plan is the "
            "preview; this command has no dry-run. Does not change current, "
            "pins, or retention, overwrite an existing snapshot id, mutate "
            "the source graph, or claim backup or recoverability. Never "
            "creates .publish.lock, and is not an MCP tool. A crash may leave "
            ".staging-<snapshot-id> and its .staging-writer.lock protocol file."
        )
    )
    parser.add_argument(
        "--source-graph",
        type=Path,
        required=True,
        help="Managed source BYOG graph root, relative to cwd.",
    )
    parser.add_argument(
        "--snapshot",
        required=True,
        help="current or a canonical retained published snapshot id.",
    )
    parser.add_argument(
        "--target-graph",
        type=Path,
        required=True,
        help="Managed target BYOG graph root, relative to cwd.",
    )
    parser.add_argument(
        "--expected-transfer-revision",
        required=True,
        help="sha256:<64 lowercase hex> from a fresh snapshot-transfer-plan",
    )
    parser.add_argument(
        "--transfer-confirmed",
        action="store_true",
        help=(
            "Required to create the staging directory and publish the "
            "snapshot. The command still refuses to copy if the recomputed "
            "transfer_revision no longer matches or the plan is blocked."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_transfer_apply_scope(
            args.source_graph,
            args.snapshot,
            args.target_graph,
            args.expected_transfer_revision,
            transfer_confirmed=bool(args.transfer_confirmed),
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            sys.stdout.flush()
            if result.get("partial") or not result.get("ok"):
                return 1
    except SnapshotTransferApplyError as error:
        print(f"snapshot-transfer-apply: {error}", file=sys.stderr)
        return error.exit_code
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ) as error:
        print(f"snapshot-transfer-apply: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
