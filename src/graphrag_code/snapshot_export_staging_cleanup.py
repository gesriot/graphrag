#!/usr/bin/env python
"""Apply one CAS-verified export-staging cleanup plan.

``snapshot-export-staging-cleanup`` applies exactly one schema-2
``snapshot-export-staging-cleanup-plan``. It deletes only the
recomputed plan's ``deletion_candidates`` and nothing else. There is
no dry-run; ``snapshot-export-staging-cleanup-plan`` is the preview.
Without ``--cleanup-confirmed`` the command exits 2 and changes
nothing. Schema-1 plan revisions are not accepted.

The command operates on an explicitly selected parent directory. It
does not inspect a managed graph, ``current``, ``snapshots/``, pins,
``.publish.lock``, an export destination, or export authenticity.
There is no graph lease. One parent descriptor plus retained
no-follow staging and lock descriptors cover plan recomputation,
revision comparison, writer-lock claims, identity revalidation,
deletion, result construction, serialization, stdout write, and
stdout flush.

The plan's ``not_held_at_scan`` observation is not this command's
destructive claim. Apply recomputes the schema-2 plan and takes
fresh nonblocking exclusive claims on each selected existing
``.export-writer.lock``. Advisory locks protect only cooperating
processes. No writer death, ownership, age, PID, process, host,
boot-id, or timeout is inferred. Recursive deletion is not
transactionally atomic. A partial result always requires a fresh
plan. There is no rollback, trash, quarantine, or recovery. MCP
stays exactly 12 read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-export-staging-cleanup --parent <directory> \\
        --expected-plan-revision sha256:<hex> --cleanup-confirmed [--json]
    python -m graphrag_code.snapshot_export_staging_cleanup --parent <directory> \\
        --expected-plan-revision sha256:<hex> --cleanup-confirmed [--json]
    uv run python scripts/snapshot_export_staging_cleanup.py --parent <directory> \\
        --expected-plan-revision sha256:<hex> --cleanup-confirmed [--json]
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from graphrag_code.snapshot_export_staging import (
    HeldExportStagingObservation,
    SnapshotExportStagingError,
    SnapshotExportStagingIntegrityError,
    held_export_staging_observation_scope,
    is_current_export_staging_name,
    is_export_staging_prefix_name,
)
from graphrag_code.snapshot_export_staging_cleanup_plan import (
    SnapshotExportStagingCleanupPlanError,
    SnapshotExportStagingCleanupPlanIntegrityError,
    _plan_from_inventory,
)
from graphrag_code.snapshot_export_writer_lease import (
    EXPORT_STAGING_WRITER_LOCK_NAME,
    ExportWriterLeaseError,
    ExportWriterLeaseIntegrityError,
    ExportWriterLeaseUnsafe,
    HeldExportWriterLease,
    claim_existing_export_writer_lease,
    prove_export_writer_lock_absent,
    require_export_writer_lock_cleanup_primitives,
)

APPLY_SCHEMA_VERSION = 1
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_ERROR_CHARS = 400
_CONFIRMATION_MESSAGE = """\
refusing to apply export staging cleanup without --cleanup-confirmed.

This is an explicit mutating CLI operation. It deletes only the
CAS-verified deletion-candidate directories named
.graphrag-export-<32 lowercase hex> under --parent. It does not
inspect a managed graph, activate, publish, change current, write
pins, repair, reindex, or verify export authenticity.
snapshot-export-staging-cleanup-plan is the preview; this command
has no dry-run mode. Confirmation is required even when the
candidate set is empty.

--expected-plan-revision is a compare-and-swap guard: the command
recomputes the complete schema-2 cleanup plan from a fresh bounded
two-scan observation of the held parent and deletes only when that
token still matches. Schema-1 plan revisions are not accepted. A
mismatched revision changes nothing. Recursive deletion is not
transactionally atomic. A crash or later-candidate failure can leave
a partially applied cleanup; there is no rollback. A partial result
always requires a fresh plan. Advisory locks protect only
cooperating processes. There is no graph lease because this operates
on an arbitrary export parent.
""".strip()
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "plan_observation_is_not_claim",
        "kind": "notice",
        "message": (
            "The plan's not_held_at_scan observation was not this "
            "destructive claim. Apply recomputes the schema-2 plan and "
            "takes fresh exclusive claims on each selected existing "
            "writer lock."
        ),
    },
    {
        "code": "advisory_locks_cooperating_only",
        "kind": "notice",
        "message": (
            "Advisory locks protect only cooperating processes. This "
            "command does not infer writer death, ownership, age, PID, "
            "process, host, boot-id, or timeout. Non-cooperating "
            "processes remain outside the protection boundary."
        ),
    },
    {
        "code": "recursive_deletion_not_atomic",
        "kind": "notice",
        "message": (
            "Recursive deletion is not transactionally atomic. A "
            "partial result always requires a fresh plan. There is no "
            "rollback, trash, quarantine, or recovery."
        ),
    },
    {
        "code": "no_graph_lease_arbitrary_parent",
        "kind": "notice",
        "message": (
            "This command operates on an arbitrary export parent. It "
            "does not take a graph lease and does not inspect a managed "
            "graph, current, snapshots/, pins, .publish.lock, an export "
            "destination, or export authenticity."
        ),
    },
    {
        "code": "cli_only_not_mcp",
        "kind": "notice",
        "message": (
            "snapshot-export-staging-cleanup is CLI-only and "
            "intentionally absent from the fixed 12-tool MCP set."
        ),
    },
)


class SnapshotExportStagingCleanupError(Exception):
    """Expected apply failure before mutation. Default exit 2."""

    exit_code = 2


class SnapshotExportStagingCleanupIntegrityError(SnapshotExportStagingCleanupError):
    """Mismatched plan revision, integrity, claim, or concurrency. Exit 1."""

    exit_code = 1


class _SnapshotExportStagingCleanupMutationError(Exception):
    """Carry a failure that occurred after at least one successful unlink."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


def parse_plan_revision(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotExportStagingCleanupError(
            "expected-plan-revision must be sha256:<64 lowercase hex>"
        )
    if value != value.strip():
        raise SnapshotExportStagingCleanupError(
            "expected-plan-revision must not contain leading or trailing "
            f"whitespace: {value!r}"
        )
    if not _REVISION_RE.fullmatch(value):
        raise SnapshotExportStagingCleanupError(
            "expected-plan-revision must be sha256:<64 lowercase hex>, "
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
    if result.get("partial"):
        return (
            "snapshot-export-staging-cleanup: PARTIAL FAILURE "
            f"parent={result.get('parent')} "
            f"deleted={result.get('deleted_count')} "
            f"failed={result.get('failed_staging_entry')} "
            "not_attempted="
            f"{len(result.get('not_attempted_staging_entries') or [])} "
            f"remaining={len(result.get('remaining_staging_entries') or [])} "
            "filesystem_may_have_changed=true "
            "retry_requires_fresh_plan=true "
            "There is no rollback; capture a fresh "
            "snapshot-export-staging-cleanup-plan before retry."
        )
    return (
        "snapshot-export-staging-cleanup: "
        f"parent={result.get('parent')} "
        f"deleted={result.get('deleted_count')} "
        f"remaining={len(result.get('remaining_staging_entries') or [])} "
        f"observed_plan_revision={result.get('observed_plan_revision')} "
        f"changed={str(result.get('changed')).lower()} "
        "filesystem_may_have_changed="
        f"{str(result.get('filesystem_may_have_changed')).lower()}"
    )


def _bounded_error(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}"
    text = " ".join(text.split())
    if len(text) > _MAX_ERROR_CHARS:
        return text[: _MAX_ERROR_CHARS - 3] + "..."
    return text


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(dict.fromkeys(values), key=os.fsencode)


def _full_identity(info: os.stat_result) -> Tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _inode_identity(info: os.stat_result) -> Tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _is_canonical_direct_name(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and Path(name).name == name
    )


def _wrap_plan_error(error: Exception) -> SnapshotExportStagingCleanupError:
    if isinstance(error, SnapshotExportStagingCleanupPlanIntegrityError):
        return SnapshotExportStagingCleanupIntegrityError(str(error))
    if isinstance(error, SnapshotExportStagingCleanupPlanError):
        wrapped = SnapshotExportStagingCleanupError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    if isinstance(error, SnapshotExportStagingIntegrityError):
        return SnapshotExportStagingCleanupIntegrityError(str(error))
    if isinstance(error, SnapshotExportStagingError):
        wrapped = SnapshotExportStagingCleanupError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    return SnapshotExportStagingCleanupError(str(error))


def _wrap_lease_error(error: Exception, name: str) -> SnapshotExportStagingCleanupError:
    if isinstance(error, ExportWriterLeaseUnsafe):
        return SnapshotExportStagingCleanupIntegrityError(str(error))
    if isinstance(error, ExportWriterLeaseIntegrityError):
        message = str(error)
        if "held by a cooperating process" in message:
            return SnapshotExportStagingCleanupIntegrityError(
                f"export writer lease is held by a cooperating process: {name}"
            )
        return SnapshotExportStagingCleanupIntegrityError(message)
    if isinstance(error, ExportWriterLeaseError):
        message = str(error)
        if "unsupported" in message:
            wrapped = SnapshotExportStagingCleanupError(message)
            wrapped.exit_code = 2
            return wrapped
        return SnapshotExportStagingCleanupIntegrityError(message)
    return SnapshotExportStagingCleanupIntegrityError(str(error))


def _plan_from_observation(
    observation: HeldExportStagingObservation,
) -> Dict[str, Any]:
    try:
        return _plan_from_inventory(observation.inventory)
    except SnapshotExportStagingCleanupPlanError as error:
        raise _wrap_plan_error(error) from error
    except SnapshotExportStagingError as error:
        raise _wrap_plan_error(error) from error


def _require_matching_revision(plan: Mapping[str, Any], expected: str) -> None:
    observed = plan.get("plan_revision")
    if observed != expected:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"expected-plan-revision {expected!r} does not match "
            f"observed {observed!r}; refusing to delete"
        )


def _staging_identity_from_entry(
    entry: Mapping[str, Any],
) -> Tuple[int, int, int, int, int, int]:
    try:
        return (
            int(entry["dev"]),
            int(entry["ino"]),
            int(entry["size"]),
            int(entry["mtime_ns"]),
            int(entry["ctime_ns"]),
            int(entry["mode"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"deletion candidate is missing a complete staging identity: "
            f"{entry.get('name')!r}"
        ) from error


def _lock_identity_from_entry(
    entry: Mapping[str, Any],
) -> Tuple[int, int, int, int, int, int]:
    try:
        return (
            int(entry["writer_lease_dev"]),
            int(entry["writer_lease_ino"]),
            int(entry["writer_lease_mode"]),
            int(entry["writer_lease_size"]),
            int(entry["writer_lease_mtime_ns"]),
            int(entry["writer_lease_ctime_ns"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"deletion candidate is missing a complete writer-lock "
            f"identity: {entry.get('name')!r}"
        ) from error


def _entry_for_name(inventory: Mapping[str, Any], name: str) -> Dict[str, Any]:
    for item in inventory.get("staging_entries") or []:
        if item.get("name") == name:
            return dict(item)
    raise SnapshotExportStagingCleanupIntegrityError(
        f"deletion candidate is missing from the recomputed inventory: "
        f"{name!r}"
    )


def _require_parent(
    observation: HeldExportStagingObservation, *, full_identity: bool
) -> None:
    path = observation.parent_path
    parent_fd = observation.parent_fd
    expected = observation.parent_identity
    try:
        held = os.fstat(parent_fd)
        current = path.lstat()
    except OSError as error:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"parent changed during cleanup: {path}"
        ) from error
    if (
        not stat.S_ISDIR(held.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _inode_identity(held) != (expected[0], expected[1])
        or _inode_identity(current) != (expected[0], expected[1])
    ):
        raise SnapshotExportStagingCleanupIntegrityError(
            f"parent changed or no longer names the held directory: {path}"
        )
    if full_identity and (
        _full_identity(held) != expected or _full_identity(current) != expected
    ):
        raise SnapshotExportStagingCleanupIntegrityError(
            f"parent changed or no longer names the held directory: {path}"
        )


def _require_staging_full(
    observation: HeldExportStagingObservation,
    name: str,
    staging_fd: int,
    expected: Tuple[int, int, int, int, int, int],
) -> os.stat_result:
    try:
        held = os.fstat(staging_fd)
        path_info = os.stat(
            name, dir_fd=observation.parent_fd, follow_symlinks=False
        )
    except FileNotFoundError as error:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"deletion candidate disappeared: {observation.parent_path / name}"
        ) from error
    except OSError as error:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"cannot re-inspect deletion candidate "
            f"{observation.parent_path / name}: {error}"
        ) from error
    if (
        not stat.S_ISDIR(held.st_mode)
        or stat.S_ISLNK(path_info.st_mode)
        or not stat.S_ISDIR(path_info.st_mode)
        or _full_identity(held) != expected
        or _full_identity(path_info) != expected
    ):
        raise SnapshotExportStagingCleanupIntegrityError(
            f"staging directory changed or became unsafe before deletion: "
            f"{observation.parent_path / name}"
        )
    return held


def _validate_export_deletion_set(
    observation: HeldExportStagingObservation, plan: Mapping[str, Any]
) -> List[str]:
    """Validate the complete candidate set. No mutation."""
    _require_parent(observation, full_identity=True)
    candidates = list(plan["deletion_candidates"])
    if candidates != _byte_sort(candidates):
        raise SnapshotExportStagingCleanupIntegrityError(
            "deletion candidates are not in canonical raw-filesystem-byte order"
        )
    if len(candidates) != len(set(candidates)):
        raise SnapshotExportStagingCleanupIntegrityError(
            "deletion candidates are not unique"
        )
    blocked_names = {
        str(item.get("name") or "")
        for item in (plan.get("blocked_entries") or [])
        if item.get("name")
    }
    unrecognized_names = {
        str(item.get("name") or "")
        for item in (observation.inventory.get("unrecognized_prefixed_entries") or [])
        if item.get("name")
    }
    if set(candidates) & blocked_names:
        raise SnapshotExportStagingCleanupIntegrityError(
            "deletion candidates overlap blocked staging entries"
        )
    if set(candidates) & unrecognized_names:
        raise SnapshotExportStagingCleanupIntegrityError(
            "deletion candidates overlap unrecognized prefixed entries"
        )
    for name in candidates:
        if not isinstance(name, str) or not is_current_export_staging_name(name):
            raise SnapshotExportStagingCleanupIntegrityError(
                f"deletion candidate is not a canonical export staging "
                f"name: {name!r}"
            )
        if is_export_staging_prefix_name(name) and not is_current_export_staging_name(
            name
        ):
            raise SnapshotExportStagingCleanupIntegrityError(
                f"deletion candidate is not a canonical export staging "
                f"name: {name!r}"
            )
        if not _is_canonical_direct_name(name):
            raise SnapshotExportStagingCleanupIntegrityError(
                f"deletion candidate is not a direct child name: {name!r}"
            )
        entry = _entry_for_name(observation.inventory, name)
        if entry.get("kind") != "directory":
            raise SnapshotExportStagingCleanupIntegrityError(
                f"deletion candidate is not a staging directory: {name!r}"
            )
        pair = observation.held.get(name)
        if pair is None or pair[0] is None:
            raise SnapshotExportStagingCleanupIntegrityError(
                f"deletion candidate has no retained staging descriptor: "
                f"{name!r}"
            )
        _require_staging_full(
            observation, name, pair[0], _staging_identity_from_entry(entry)
        )
    return candidates


def _claim_candidate(
    observation: HeldExportStagingObservation,
    name: str,
    entry: Mapping[str, Any],
) -> HeldExportWriterLease:
    pair = observation.held.get(name)
    if pair is None or pair[0] is None or pair[1] is None:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"deletion candidate has no retained lock descriptor: {name!r}"
        )
    staging_fd, lock_fd = pair
    staging_identity = _staging_identity_from_entry(entry)
    try:
        claim = claim_existing_export_writer_lease(
            parent_fd=observation.parent_fd,
            staging_name=name,
            staging_fd=staging_fd,
            staging_identity=(staging_identity[0], staging_identity[1]),
            lock_fd=lock_fd,
            expected_lock_identity=_lock_identity_from_entry(entry),
            staging_path=observation.parent_path / name,
        )
    except ExportWriterLeaseError as error:
        raise _wrap_lease_error(error, name) from error
    observation.held[name] = (staging_fd, None)
    return claim


def _revalidate_claim(
    observation: HeldExportStagingObservation,
    name: str,
    claim: HeldExportWriterLease,
    entry: Mapping[str, Any],
    *,
    parent_full_identity: bool,
) -> None:
    _require_parent(observation, full_identity=parent_full_identity)
    if claim.closed:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"writer-lock claim is not held for {name!r}"
        )
    expected_staging = _staging_identity_from_entry(entry)
    _require_staging_full(observation, name, claim.staging_fd, expected_staging)
    if claim.staging_fd != observation.held.get(name, (None, None))[0]:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"writer-lock claim is not held for {name!r}"
        )
    expected_lock = _lock_identity_from_entry(entry)
    if claim.lock_identity != expected_lock:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"writer-lock identity changed before deletion: "
            f"{observation.parent_path / name / EXPORT_STAGING_WRITER_LOCK_NAME}"
        )
    try:
        claim.revalidate()
    except ExportWriterLeaseError as error:
        raise _wrap_lease_error(error, name) from error
    lease_state = entry.get("writer_lease_state")
    if (
        lease_state != "not_held_at_scan"
        or entry.get("writer_lease_metadata_present") is not True
        or entry.get("writer_lease_contended") is not False
    ):
        raise SnapshotExportStagingCleanupIntegrityError(
            f"deletion candidate no longer matches cooperative leftover "
            f"conditions: {name!r}"
        )


def _has_o_nofollow() -> bool:
    """Test hook. Recursive deletion uses O_NOFOLLOW when this is true."""
    return hasattr(os, "O_NOFOLLOW")


def _open_directory_nofollow(dir_fd: int, name: str) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if _has_o_nofollow():
        flags |= os.O_NOFOLLOW
    else:
        raise SnapshotExportStagingCleanupError(
            "safe descriptor-relative no-follow directory deletion is "
            f"unsupported on this platform: {sys.platform!r}"
        )
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as error:
        if error.errno in {
            errno.ELOOP,
            errno.ENOENT,
            errno.ENOTDIR,
            getattr(errno, "EPERM", errno.EACCES),
        }:
            raise SnapshotExportStagingCleanupIntegrityError(
                f"staging path changed or became unsafe while deleting it: "
                f"{name}"
            ) from error
        raise SnapshotExportStagingCleanupError(
            f"cannot open staging path for deletion {name}: {error}"
        ) from error


def _list_fd_names(fd: int) -> List[str]:
    if os.scandir not in getattr(os, "supports_fd", set()):
        raise SnapshotExportStagingCleanupError(
            "safe descriptor-relative directory deletion is unsupported "
            f"on this platform: {sys.platform!r}"
        )
    names: List[str] = []
    with os.scandir(fd) as iterator:
        for entry in iterator:
            names.append(entry.name)
    return names


def _remove_child_nofollow(dir_fd: int, name: str) -> bool:
    """Remove one direct child without following links. Report mutation."""
    if not _is_canonical_direct_name(name):
        raise SnapshotExportStagingCleanupIntegrityError(
            f"unsafe staging child path: {name!r}"
        )
    try:
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"cannot inspect staging child {name}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        try:
            os.unlink(name, dir_fd=dir_fd)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise SnapshotExportStagingCleanupIntegrityError(
                f"cannot unlink staging child {name}: {error}"
            ) from error
        return True
    child_fd = _open_directory_nofollow(dir_fd, name)
    mutated = False
    try:
        opened = os.fstat(child_fd)
        try:
            current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotExportStagingCleanupIntegrityError(
                f"staging path changed while deleting it: {name}"
            ) from error
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or _inode_identity(opened) != _inode_identity(before)
            or _inode_identity(current) != _inode_identity(opened)
        ):
            raise SnapshotExportStagingCleanupIntegrityError(
                f"staging path changed or became unsafe while deleting it: "
                f"{name}"
            )
        names = _list_fd_names(child_fd)
        for child in sorted(names, key=os.fsencode):
            if child in {".", ".."}:
                continue
            mutated = _remove_child_nofollow(child_fd, child) or mutated
        try:
            current = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotExportStagingCleanupIntegrityError(
                f"staging path changed while deleting it: {name}"
            ) from error
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or _inode_identity(current) != _inode_identity(opened)
        ):
            raise SnapshotExportStagingCleanupIntegrityError(
                f"staging path changed or became unsafe while deleting it: "
                f"{name}"
            )
        os.rmdir(name, dir_fd=dir_fd)
        return True
    except _SnapshotExportStagingCleanupMutationError:
        raise
    except Exception as error:
        if mutated:
            raise _SnapshotExportStagingCleanupMutationError(error) from error
        raise
    finally:
        os.close(child_fd)


def _after_cleanup_plan_recompute(
    observation: HeldExportStagingObservation, plan: Mapping[str, Any]
) -> None:
    """Test hook. Called after the schema-2 plan recompute."""
    return


def _after_cleanup_writer_claims(claims: Sequence[HeldExportWriterLease]) -> None:
    """Test hook. Called after every candidate writer lock is claimed."""
    return


def _after_cleanup_revalidation(
    observation: HeldExportStagingObservation,
    claims: Sequence[HeldExportWriterLease],
) -> None:
    """Test hook. Called after every candidate is revalidated."""
    return


def _before_cleanup_deletion(observation: HeldExportStagingObservation) -> None:
    """Test hook. Called after revalidation and before the first unlink."""
    return


def _remove_claimed_staging_entry(
    observation: HeldExportStagingObservation,
    name: str,
    claim: HeldExportWriterLease,
    entry: Mapping[str, Any],
) -> None:
    """Delete one claimed staging directory. Tests monkeypatch this."""
    expected = _staging_identity_from_entry(entry)
    _require_staging_full(observation, name, claim.staging_fd, expected)
    if claim.closed:
        raise SnapshotExportStagingCleanupIntegrityError(
            f"writer-lock claim was released before payload deletion: {name}"
        )
    mutated = False
    try:
        names = _list_fd_names(claim.staging_fd)
        for child in sorted(names, key=os.fsencode):
            if child in {".", ".."}:
                continue
            if child == EXPORT_STAGING_WRITER_LOCK_NAME:
                continue
            if claim.closed:
                raise SnapshotExportStagingCleanupIntegrityError(
                    f"writer-lock claim was released before payload deletion: "
                    f"{name}"
                )
            mutated = _remove_child_nofollow(claim.staging_fd, child) or mutated
        if claim.closed:
            raise SnapshotExportStagingCleanupIntegrityError(
                f"writer-lock claim was released before lock removal: {name}"
            )
        # From this point an error can be raised after the lock pathname
        # has already been unlinked (for example by a post-unlink hook or
        # a future durability check inside ``release_and_remove``).  Mark
        # the operation as possibly mutating before entering that boundary
        # so such failures can never be reported as empty-stdout
        # pre-mutation integrity failures.
        mutated = True
        try:
            claim.release_and_remove()
        except ExportWriterLeaseError as error:
            raise _wrap_lease_error(error, name) from error
        prove_export_writer_lock_absent(
            claim.staging_fd, observation.parent_path / name
        )
        _require_parent(observation, full_identity=False)
        try:
            held = os.fstat(claim.staging_fd)
            path_info = os.stat(
                name, dir_fd=observation.parent_fd, follow_symlinks=False
            )
        except FileNotFoundError as error:
            raise SnapshotExportStagingCleanupIntegrityError(
                f"staging directory disappeared before removal: "
                f"{observation.parent_path / name}"
            ) from error
        except OSError as error:
            raise SnapshotExportStagingCleanupIntegrityError(
                f"cannot inspect staging directory before removal "
                f"{observation.parent_path / name}: {error}"
            ) from error
        if (
            not stat.S_ISDIR(held.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or not stat.S_ISDIR(path_info.st_mode)
            or _inode_identity(held) != (expected[0], expected[1])
            or _inode_identity(path_info) != (expected[0], expected[1])
        ):
            raise SnapshotExportStagingCleanupIntegrityError(
                f"staging directory identity changed before removal: "
                f"{observation.parent_path / name}"
            )
        leftover = [
            child
            for child in _list_fd_names(claim.staging_fd)
            if child not in {".", ".."}
        ]
        if leftover:
            raise SnapshotExportStagingCleanupIntegrityError(
                f"staging directory is not empty after payload removal: "
                f"{observation.parent_path / name}"
            )
        try:
            os.rmdir(name, dir_fd=observation.parent_fd)
        except OSError as error:
            raise SnapshotExportStagingCleanupIntegrityError(
                f"cannot remove staging directory "
                f"{observation.parent_path / name}: {error}"
            ) from error
        try:
            os.stat(name, dir_fd=observation.parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise SnapshotExportStagingCleanupIntegrityError(
                f"staging directory is still present after removal: "
                f"{observation.parent_path / name}"
            )
    except _SnapshotExportStagingCleanupMutationError:
        raise
    except Exception as error:
        if mutated:
            raise _SnapshotExportStagingCleanupMutationError(error) from error
        raise


def _remaining_after(
    plan: Mapping[str, Any],
    *,
    failed: Optional[str],
    not_attempted: Sequence[str],
) -> List[str]:
    remaining = [
        str(item.get("name") or "")
        for item in (plan.get("blocked_entries") or [])
        if item.get("name")
    ]
    if failed is not None:
        remaining.append(failed)
    remaining.extend(str(name) for name in not_attempted)
    return _byte_sort(remaining)


def _result(
    plan: Mapping[str, Any],
    *,
    expected: str,
    deleted: Sequence[str],
    failed_staging_entry: Optional[str],
    not_attempted: Sequence[str],
    error: Optional[str],
    partial: bool,
) -> Dict[str, Any]:
    deleted_ids = list(deleted)
    remaining = _remaining_after(
        plan, failed=failed_staging_entry, not_attempted=not_attempted
    )
    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "ok": not partial,
        "parent": plan["parent"],
        "expected_plan_revision": expected,
        "observed_plan_revision": plan["plan_revision"],
        "planned_deletion_candidates": list(plan["deletion_candidates"]),
        "deleted_staging_entries": deleted_ids,
        "deleted_count": len(deleted_ids),
        "remaining_staging_entries": remaining,
        "changed": bool(deleted_ids),
        "cleanup_confirmed": True,
        "partial": partial,
        "filesystem_may_have_changed": bool(deleted_ids) or partial,
        "retry_requires_fresh_plan": partial,
        "failed_staging_entry": failed_staging_entry,
        "not_attempted_staging_entries": list(not_attempted),
        "ownership_inference": False,
        "error": error,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }


def _cleanup_with_observation(
    observation: HeldExportStagingObservation,
    expected: str,
    claims: List[HeldExportWriterLease],
) -> Dict[str, Any]:
    plan = _plan_from_observation(observation)
    _after_cleanup_plan_recompute(observation, plan)
    _require_matching_revision(plan, expected)
    candidates = _validate_export_deletion_set(observation, plan)
    if not candidates:
        return _result(
            plan,
            expected=expected,
            deleted=[],
            failed_staging_entry=None,
            not_attempted=[],
            error=None,
            partial=False,
        )

    entries = [_entry_for_name(observation.inventory, name) for name in candidates]
    for name, entry in zip(candidates, entries):
        claims.append(_claim_candidate(observation, name, entry))
    _after_cleanup_writer_claims(claims)
    if len(claims) != len(candidates):
        raise SnapshotExportStagingCleanupIntegrityError(
            "writer-lock claim count does not match deletion candidates"
        )
    for name, claim, entry in zip(candidates, claims, entries):
        _revalidate_claim(
            observation,
            name,
            claim,
            entry,
            parent_full_identity=True,
        )
    _after_cleanup_revalidation(observation, claims)
    _before_cleanup_deletion(observation)

    deleted: List[str] = []
    for index, (name, claim, entry) in enumerate(
        zip(candidates, claims, entries)
    ):
        try:
            _revalidate_claim(
                observation,
                name,
                claim,
                entry,
                parent_full_identity=not deleted,
            )
        except Exception as error:
            if deleted:
                return _result(
                    plan,
                    expected=expected,
                    deleted=deleted,
                    failed_staging_entry=name,
                    not_attempted=candidates[index + 1 :],
                    error=_bounded_error(error),
                    partial=True,
                )
            if isinstance(error, SnapshotExportStagingCleanupError):
                raise
            raise SnapshotExportStagingCleanupIntegrityError(str(error)) from error
        try:
            _remove_claimed_staging_entry(observation, name, claim, entry)
        except _SnapshotExportStagingCleanupMutationError as error:
            return _result(
                plan,
                expected=expected,
                deleted=deleted,
                failed_staging_entry=name,
                not_attempted=candidates[index + 1 :],
                error=_bounded_error(error.cause),
                partial=True,
            )
        except Exception as error:
            if (
                not deleted
                and isinstance(error, SnapshotExportStagingCleanupIntegrityError)
            ):
                raise
            return _result(
                plan,
                expected=expected,
                deleted=deleted,
                failed_staging_entry=name,
                not_attempted=candidates[index + 1 :],
                error=_bounded_error(error),
                partial=True,
            )
        deleted.append(name)
    return _result(
        plan,
        expected=expected,
        deleted=deleted,
        failed_staging_entry=None,
        not_attempted=[],
        error=None,
        partial=False,
    )


@contextmanager
def _snapshot_export_staging_cleanup_scope(
    parent: object,
    expected_plan_revision: object,
    *,
    cleanup_confirmed: bool,
) -> Iterator[Dict[str, Any]]:
    """Yield one apply result while parent descriptors and claims stay held."""
    if not cleanup_confirmed:
        raise SnapshotExportStagingCleanupError(_CONFIRMATION_MESSAGE)
    expected = parse_plan_revision(expected_plan_revision)
    try:
        require_export_writer_lock_cleanup_primitives()
    except ExportWriterLeaseError as error:
        raise SnapshotExportStagingCleanupError(str(error)) from error
    claims: List[HeldExportWriterLease] = []
    try:
        with held_export_staging_observation_scope(parent) as observation:
            try:
                yield _cleanup_with_observation(observation, expected, claims)
            finally:
                for claim in claims:
                    claim.close()
    except SnapshotExportStagingCleanupError:
        raise
    except SnapshotExportStagingError as error:
        raise _wrap_plan_error(error) from error
    except ExportWriterLeaseError as error:
        raise _wrap_lease_error(error, "") from error


def snapshot_export_staging_cleanup(
    parent: Path,
    expected_plan_revision: str,
    *,
    cleanup_confirmed: bool,
) -> Dict[str, Any]:
    """Apply one CAS-verified export-staging cleanup plan.

    Returns the stable result object. Complete success has ``ok=true``.
    Partial mutation has ``ok=false`` and ``partial=true`` and does not
    raise; the CLI still exits 1 after emitting that result. Pre-deletion
    failures raise :class:`SnapshotExportStagingCleanupError` (exit 2) or
    :class:`SnapshotExportStagingCleanupIntegrityError` (exit 1).
    """
    with _snapshot_export_staging_cleanup_scope(
        parent,
        expected_plan_revision,
        cleanup_confirmed=cleanup_confirmed,
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Delete the CAS-verified snapshot-export-staging-cleanup-plan "
            "deletion candidates. Requires --cleanup-confirmed and "
            "--expected-plan-revision. snapshot-export-staging-cleanup-plan "
            "is the preview; this command has no dry-run. Accepts schema-2 "
            "plan revisions only. Does not take a graph lease and is not "
            "an MCP tool. Recursive deletion is not transactionally atomic."
        )
    )
    parser.add_argument(
        "--parent",
        type=Path,
        required=True,
        help="Parent directory to clean, relative to cwd.",
    )
    parser.add_argument(
        "--expected-plan-revision",
        required=True,
        help="sha256:<64 lowercase hex> from snapshot-export-staging-cleanup-plan",
    )
    parser.add_argument(
        "--cleanup-confirmed",
        action="store_true",
        help=(
            "Required to delete CAS-verified deletion-candidate "
            "directories. The command still refuses to delete if the "
            "recomputed plan_revision no longer matches."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_export_staging_cleanup_scope(
            args.parent,
            args.expected_plan_revision,
            cleanup_confirmed=bool(args.cleanup_confirmed),
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            sys.stdout.flush()
            if result.get("partial") or not result.get("ok"):
                return 1
    except SnapshotExportStagingCleanupError as error:
        print(f"snapshot-export-staging-cleanup: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-export-staging-cleanup: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
