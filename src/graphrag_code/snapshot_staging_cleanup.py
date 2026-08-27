#!/usr/bin/env python
"""Apply one CAS-verified staging cleanup plan.

``snapshot-staging-cleanup`` applies exactly one schema-2
``snapshot-staging-cleanup-plan``. It deletes only the recomputed
plan's ``deletion_candidates`` and nothing else. There is no dry-run;
``snapshot-staging-cleanup-plan`` is the preview. Without
``--cleanup-confirmed`` the command exits 2 and changes nothing.

The command requires an already-adopted regular ``.publish.lock`` and
never creates, truncates, rewrites, chmods, or replaces that lock. It
never creates or changes ``.snapshot-pins.json``, ``current``, or any
published snapshot. One exclusive existing-lock lease covers plan
recomputation, revision comparison, writer-lock claims, identity
revalidation, deletion, result construction, serialization, stdout
write, and stdout flush. It does not take a nested shared graph lease
and does not call the unguarded public retention cleanup helper.

The plan's ``not_held_at_scan`` observation is not this command's
destructive claim. Apply recomputes the schema-2 plan and takes fresh
nonblocking exclusive claims on each selected existing writer lock.
Advisory locks protect only cooperating processes. No writer death,
ownership, age, PID, process, host, boot-id, or timeout is inferred.
Recursive deletion is not transactionally atomic. A partial result
always requires a fresh plan. There is no rollback, trash, quarantine,
or recovery. MCP stays exactly 14 read-only tools; this command is
CLI-only.

Usage:
    graphrag-code snapshot-staging-cleanup --graph <root> \\
        --expected-plan-revision sha256:<hex> --cleanup-confirmed [--json]
    python -m graphrag_code.snapshot_staging_cleanup --graph <root> \\
        --expected-plan-revision sha256:<hex> --cleanup-confirmed [--json]
    uv run python scripts/snapshot_staging_cleanup.py --graph <root> \\
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

from graphrag_code.byog_graph import (
    PUBLICATION_LOCK_NAME,
    STAGING_NAME_PREFIX,
    STAGING_WRITER_LOCK_NAME,
    ByogPublicationLockError,
    ByogReaderLockError,
    HeldExistingStagingWriterClaim,
    StagingWriterLeaseError,
    StagingWriterLockContention,
    StagingWriterLockUnsafe,
    _validate_managed_snapshot_layout,
    _validate_staging_writer_lock_fd,
    acquire_existing_staging_writer_claim,
    graph_exclusive_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
)
from graphrag_code.snapshot_staging import (
    SnapshotStagingError,
    SnapshotStagingIntegrityError,
    staging_structure_token,
)
from graphrag_code.snapshot_staging_cleanup_plan import (
    SnapshotStagingCleanupPlanError,
    SnapshotStagingCleanupPlanIntegrityError,
    build_stable_cleanup_plan_unlocked,
)

APPLY_SCHEMA_VERSION = 1
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_ERROR_CHARS = 400
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_CONFIRMATION_MESSAGE = """\
refusing to apply staging cleanup without --cleanup-confirmed.

This is an explicit mutating CLI operation. It deletes only the
CAS-verified deletion-candidate directories under
<graph>/snapshots/.staging-*. It does not activate, publish, change
current, write the pin registry, repair, or reindex.
snapshot-staging-cleanup-plan is the preview; this command has no
dry-run mode. Confirmation is required even when the candidate set is
empty.

--expected-plan-revision is a compare-and-swap guard: the exclusive
existing-lock lease recomputes the complete schema-2 cleanup plan and
deletes only when that token still matches. Schema-1 plan revisions
are not accepted. A mismatched revision changes nothing. Recursive deletion is not transactionally atomic. A
crash or later-candidate failure can leave a partially applied
cleanup; there is no rollback. A partial result always requires a
fresh plan. Advisory locks protect only cooperating processes.
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
            "process, host, boot-id, or timeout."
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
        "code": "cli_only_not_mcp",
        "kind": "notice",
        "message": (
            "snapshot-staging-cleanup is CLI-only and intentionally "
            "absent from the fixed 14-tool MCP set."
        ),
    },
)


class SnapshotStagingCleanupError(Exception):
    """Expected apply failure before mutation. Default exit 2."""

    exit_code = 2


class SnapshotStagingCleanupIntegrityError(SnapshotStagingCleanupError):
    """Mismatched plan revision, integrity, claim, or concurrency. Exit 1."""

    exit_code = 1


class _SnapshotStagingCleanupMutationError(Exception):
    """Carry a failure that occurred after at least one successful unlink."""

    def __init__(self, cause: BaseException) -> None:
        super().__init__(str(cause))
        self.cause = cause


def parse_plan_revision(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotStagingCleanupError(
            "expected-plan-revision must be sha256:<64 lowercase hex>"
        )
    if value != value.strip():
        raise SnapshotStagingCleanupError(
            "expected-plan-revision must not contain leading or trailing "
            f"whitespace: {value!r}"
        )
    if not _REVISION_RE.fullmatch(value):
        raise SnapshotStagingCleanupError(
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
            "snapshot-staging-cleanup: PARTIAL FAILURE "
            f"graph={result.get('graph')} "
            f"deleted={result.get('deleted_count')} "
            f"failed={result.get('failed_staging_entry')} "
            "not_attempted="
            f"{len(result.get('not_attempted_staging_entries') or [])} "
            f"remaining={len(result.get('remaining_staging_entries') or [])} "
            "filesystem_may_have_changed=true "
            "retry_requires_fresh_plan=true "
            "There is no rollback; capture a fresh "
            "snapshot-staging-cleanup-plan before retry."
        )
    return (
        "snapshot-staging-cleanup: "
        f"graph={result.get('graph')} "
        f"current={result.get('current')} "
        f"deleted={result.get('deleted_count')} "
        f"remaining={len(result.get('remaining_staging_entries') or [])} "
        f"observed_plan_revision={result.get('observed_plan_revision')} "
        f"changed={str(result.get('changed')).lower()} "
        "filesystem_may_have_changed="
        f"{str(result.get('filesystem_may_have_changed')).lower()}"
    )


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotStagingCleanupError(
            f"graph root does not exist: {path}"
        ) from error
    except OSError as error:
        raise SnapshotStagingCleanupError(
            f"cannot inspect graph root {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotStagingCleanupError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotStagingCleanupError(
            f"graph root is not a real directory: {path}"
        )
    return path.resolve()


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotStagingCleanupError(str(error)) from error
    if not managed:
        raise SnapshotStagingCleanupError(
            "legacy flat-parquet directory has no managed snapshot staging "
            f"to clean: {root}"
        )


def _lock_error(error: Exception) -> SnapshotStagingCleanupError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotStagingCleanupError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotStagingCleanupIntegrityError(message)
    return SnapshotStagingCleanupError(message)


def _bounded_error(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}"
    text = " ".join(text.split())
    if len(text) > _MAX_ERROR_CHARS:
        return text[: _MAX_ERROR_CHARS - 3] + "..."
    return text


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(dict.fromkeys(values), key=lambda item: item.encode("utf-8"))


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _wrap_plan_error(error: Exception) -> SnapshotStagingCleanupError:
    if isinstance(error, SnapshotStagingCleanupPlanIntegrityError):
        return SnapshotStagingCleanupIntegrityError(str(error))
    if isinstance(error, SnapshotStagingCleanupPlanError):
        wrapped = SnapshotStagingCleanupError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    if isinstance(error, SnapshotStagingIntegrityError):
        return SnapshotStagingCleanupIntegrityError(str(error))
    if isinstance(error, SnapshotStagingError):
        wrapped = SnapshotStagingCleanupError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    return SnapshotStagingCleanupError(str(error))


def _plan_or_raise(root: Path) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    try:
        return build_stable_cleanup_plan_unlocked(root)
    except SnapshotStagingCleanupPlanError as error:
        raise _wrap_plan_error(error) from error
    except SnapshotStagingError as error:
        raise _wrap_plan_error(error) from error


def _require_matching_revision(plan: Mapping[str, Any], expected: str) -> None:
    observed = plan.get("plan_revision")
    if observed != expected:
        raise SnapshotStagingCleanupIntegrityError(
            f"expected-plan-revision {expected!r} does not match "
            f"observed {observed!r}; refusing to delete"
        )


def _assert_real_snapshots_dir(snapshots_dir: Path) -> None:
    try:
        info = snapshots_dir.lstat()
    except FileNotFoundError as error:
        raise SnapshotStagingCleanupError(
            f"snapshots directory missing: {snapshots_dir}"
        ) from error
    except OSError as error:
        raise SnapshotStagingCleanupError(
            f"cannot inspect snapshots directory {snapshots_dir}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotStagingCleanupError(
            f"unsafe symlinked snapshots directory: {snapshots_dir}"
        )
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotStagingCleanupError(
            f"snapshots path is not a real directory: {snapshots_dir}"
        )


def _staging_path(snapshots_dir: Path, name: str) -> Path:
    if not isinstance(name, str) or not is_staging_snapshot_name(name):
        raise SnapshotStagingCleanupError(
            f"deletion candidate is not a staging name: {name!r}"
        )
    if is_published_snapshot_id(name):
        raise SnapshotStagingCleanupError(
            f"published snapshot id is not a staging path: {name!r}"
        )
    if name in {PUBLICATION_LOCK_NAME, ".snapshot-pins.json", "current"}:
        raise SnapshotStagingCleanupError(
            f"protected path is not a staging candidate: {name!r}"
        )
    target = snapshots_dir / name
    if target.name != name or target.parent != snapshots_dir:
        raise SnapshotStagingCleanupError(f"unsafe staging path: {name!r}")
    return target


def _assert_direct_staging_directory(snapshots_dir: Path, name: str) -> Path:
    target = _staging_path(snapshots_dir, name)
    try:
        info = target.lstat()
    except FileNotFoundError as error:
        raise SnapshotStagingCleanupIntegrityError(
            f"deletion candidate is not a staging directory: {target}"
        ) from error
    except OSError as error:
        raise SnapshotStagingCleanupIntegrityError(
            f"cannot inspect deletion candidate {target}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotStagingCleanupIntegrityError(
            f"unsafe symlinked staging entry: {target}"
        )
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotStagingCleanupIntegrityError(
            f"deletion candidate is not a real directory: {target}"
        )
    return target


def _token_for_name(consistency: Mapping[str, Any], name: str) -> Dict[str, Any]:
    for item in consistency.get("staging") or []:
        if item.get("name") == name:
            return dict(item)
    raise SnapshotStagingCleanupIntegrityError(
        f"deletion candidate is missing from the recomputed consistency "
        f"state: {name!r}"
    )


def _structure_from_token(token: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "name": token["name"],
        "entry_kind": token["entry_kind"],
        "dev": token["dev"],
        "ino": token["ino"],
        "mode": token["mode"],
        "mtime_ns": token["mtime_ns"],
        "size": token["size"],
        "children": list(token.get("children") or []),
    }


def _wrap_claim_error(error: Exception, name: str) -> SnapshotStagingCleanupError:
    if isinstance(error, StagingWriterLockContention):
        return SnapshotStagingCleanupIntegrityError(
            f"staging writer lease is held by a cooperating process: {name}"
        )
    if isinstance(error, StagingWriterLockUnsafe):
        return SnapshotStagingCleanupIntegrityError(str(error))
    if isinstance(error, StagingWriterLeaseError):
        message = str(error)
        if "unsupported" in message:
            wrapped = SnapshotStagingCleanupError(message)
            wrapped.exit_code = 2
            return wrapped
        return SnapshotStagingCleanupIntegrityError(message)
    return SnapshotStagingCleanupIntegrityError(str(error))


def _claim_candidate(
    snapshots_dir: Path, name: str
) -> HeldExistingStagingWriterClaim:
    target = _assert_direct_staging_directory(snapshots_dir, name)
    try:
        return acquire_existing_staging_writer_claim(target)
    except StagingWriterLeaseError as error:
        raise _wrap_claim_error(error, name) from error


def _revalidate_claim(
    snapshots_dir: Path,
    name: str,
    claim: HeldExistingStagingWriterClaim,
    expected: Mapping[str, Any],
) -> None:
    target = _assert_direct_staging_directory(snapshots_dir, name)
    if claim.stage_dir != target or claim.closed:
        raise SnapshotStagingCleanupIntegrityError(
            f"writer-lock claim is not held for {name!r}"
        )
    try:
        observed = staging_structure_token(target)
    except SnapshotStagingIntegrityError as error:
        raise SnapshotStagingCleanupIntegrityError(str(error)) from error
    except SnapshotStagingError as error:
        raise _wrap_plan_error(error) from error
    if observed != _structure_from_token(expected):
        raise SnapshotStagingCleanupIntegrityError(
            f"staging structure changed before deletion: {target}"
        )
    lease = expected.get("writer_lease") or {}
    if (
        lease.get("protocol") != "cooperative_v1"
        or lease.get("state") != "not_held_at_scan"
        or lease.get("present") is not True
        or lease.get("regular") is not True
    ):
        raise SnapshotStagingCleanupIntegrityError(
            f"deletion candidate no longer matches cooperative leftover "
            f"conditions: {name!r}"
        )
    if claim.identity != tuple(lease.get("identity") or ()):
        raise SnapshotStagingCleanupIntegrityError(
            f"writer-lock identity changed before deletion: {claim.lock_path}"
        )
    try:
        opened = _validate_staging_writer_lock_fd(claim.fd, claim.lock_path)
    except StagingWriterLockUnsafe as error:
        raise SnapshotStagingCleanupIntegrityError(str(error)) from error
    if (opened.st_dev, opened.st_ino) != claim.inode_identity:
        raise SnapshotStagingCleanupIntegrityError(
            f"writer-lock identity changed before deletion: {claim.lock_path}"
        )


def _has_o_nofollow() -> bool:
    """Test hook. Recursive deletion uses O_NOFOLLOW when this is true."""
    return hasattr(os, "O_NOFOLLOW")


def _remove_path_nofollow(path: Path) -> bool:
    """Remove a path without following links; report whether it changed."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        path.unlink()
        return True
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if _has_o_nofollow():
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(path), flags)
    except OSError as error:
        if error.errno in {
            errno.ELOOP,
            errno.ENOENT,
            errno.ENOTDIR,
            getattr(errno, "EPERM", errno.EACCES),
        }:
            raise SnapshotStagingCleanupIntegrityError(
                f"staging path changed or became unsafe while deleting it: "
                f"{path}"
            ) from error
        raise SnapshotStagingCleanupError(
            f"cannot open staging path for deletion {path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = path.lstat()
        except OSError as error:
            raise SnapshotStagingCleanupIntegrityError(
                f"staging path changed while deleting it: {path}"
            ) from error
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise SnapshotStagingCleanupIntegrityError(
                f"staging path changed or became unsafe while deleting it: "
                f"{path}"
            )
        if os.scandir not in getattr(os, "supports_fd", set()):
            raise SnapshotStagingCleanupError(
                "safe descriptor-relative directory deletion is unsupported "
                f"on this platform: {sys.platform!r}"
            )
        names: List[str] = []
        with os.scandir(fd) as iterator:
            for entry in iterator:
                names.append(entry.name)
    finally:
        os.close(fd)
    mutated = False
    try:
        for name in sorted(names, key=lambda item: item.encode("utf-8")):
            if name in {".", ".."}:
                continue
            child = path / name
            if child.name != name or child.parent != path:
                raise SnapshotStagingCleanupIntegrityError(
                    f"unsafe staging child path: {child}"
                )
            mutated = _remove_path_nofollow(child) or mutated
        path.rmdir()
        return True
    except _SnapshotStagingCleanupMutationError:
        raise
    except Exception as error:
        if mutated:
            raise _SnapshotStagingCleanupMutationError(error) from error
        raise


def _after_cleanup_plan_recompute(
    root: Path, plan: Mapping[str, Any], consistency: Mapping[str, Any]
) -> None:
    """Test hook. Called after the exclusive-lease plan recompute."""
    return


def _after_cleanup_writer_claims(
    claims: Sequence[HeldExistingStagingWriterClaim],
) -> None:
    """Test hook. Called after every candidate writer lock is claimed."""
    return


def _after_cleanup_revalidation(
    root: Path, claims: Sequence[HeldExistingStagingWriterClaim]
) -> None:
    """Test hook. Called after every candidate is revalidated."""
    return


def _before_cleanup_deletion(root: Path) -> None:
    """Test hook. Called after revalidation and before the first unlink."""
    return


def _remove_claimed_staging_entry(
    snapshots_dir: Path,
    name: str,
    claim: HeldExistingStagingWriterClaim,
    expected: Mapping[str, Any],
) -> None:
    """Delete one claimed staging directory. Tests monkeypatch this."""
    target = _assert_direct_staging_directory(snapshots_dir, name)
    if (
        (expected.get("dev"), expected.get("ino"))
        != (target.lstat().st_dev, target.lstat().st_ino)
    ):
        raise SnapshotStagingCleanupIntegrityError(
            f"staging directory identity changed before deletion: {target}"
        )
    try:
        observed = staging_structure_token(target)
    except SnapshotStagingError as error:
        raise _wrap_plan_error(error) from error
    if observed != _structure_from_token(expected):
        raise SnapshotStagingCleanupIntegrityError(
            f"staging structure changed before deletion: {target}"
        )
    children = list(observed.get("children") or [])
    mutated = False
    try:
        for child in children:
            child_name = str(child.get("name") or "")
            if child_name == STAGING_WRITER_LOCK_NAME:
                continue
            if claim.closed:
                raise SnapshotStagingCleanupIntegrityError(
                    f"writer-lock claim was released before payload deletion: "
                    f"{name}"
                )
            child_path = target / child_name
            if child_path.name != child_name or child_path.parent != target:
                raise SnapshotStagingCleanupIntegrityError(
                    f"unsafe staging child path: {child_path}"
                )
            mutated = _remove_path_nofollow(child_path) or mutated
        if claim.closed:
            raise SnapshotStagingCleanupIntegrityError(
                f"writer-lock claim was released before lock removal: {name}"
            )
        try:
            claim.release_and_remove()
        except StagingWriterLeaseError as error:
            raise SnapshotStagingCleanupIntegrityError(str(error)) from error
        mutated = True
        try:
            leftover = target / STAGING_WRITER_LOCK_NAME
            leftover.lstat()
        except FileNotFoundError:
            pass
        else:
            raise SnapshotStagingCleanupIntegrityError(
                f"staging writer lock still present after release: {leftover}"
            )
        target.rmdir()
        if _lexists(target):
            raise SnapshotStagingCleanupIntegrityError(
                f"staging directory is still present after removal: {target}"
            )
    except _SnapshotStagingCleanupMutationError:
        raise
    except Exception as error:
        if mutated:
            raise _SnapshotStagingCleanupMutationError(error) from error
        raise


def _remaining_after(
    plan: Mapping[str, Any],
    *,
    deleted: Sequence[str],
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
        plan,
        deleted=deleted_ids,
        failed=failed_staging_entry,
        not_attempted=not_attempted,
    )
    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "ok": not partial,
        "graph": plan["graph"],
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
        "error": error,
        "current": plan["current"],
        "published_snapshots": list(plan["published_snapshots"]),
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }


def _validate_staging_deletion_set(
    root: Path, plan: Mapping[str, Any]
) -> List[str]:
    """Validate the complete staging deletion set. No mutation."""
    snapshots_dir = root / "snapshots"
    _assert_real_snapshots_dir(snapshots_dir)
    candidates = list(plan["deletion_candidates"])
    if candidates != _byte_sort(candidates):
        raise SnapshotStagingCleanupIntegrityError(
            "deletion candidates are not in canonical UTF-8-byte order"
        )
    blocked_names = {
        str(item.get("name") or "")
        for item in (plan.get("blocked_entries") or [])
        if item.get("name")
    }
    if set(candidates) & blocked_names:
        raise SnapshotStagingCleanupIntegrityError(
            "deletion candidates overlap blocked staging entries"
        )
    current = plan["current"]
    if current in candidates:
        raise SnapshotStagingCleanupIntegrityError(
            f"current snapshot is a deletion candidate: {current!r}"
        )
    published = set(plan["published_snapshots"])
    for name in candidates:
        if name in published:
            raise SnapshotStagingCleanupIntegrityError(
                f"published snapshot is a deletion candidate: {name!r}"
            )
        suffix = name[len(STAGING_NAME_PREFIX) :]
        if not is_published_snapshot_id(suffix):
            raise SnapshotStagingCleanupIntegrityError(
                f"deletion candidate suffix is not a published id: {name!r}"
            )
        _assert_direct_staging_directory(snapshots_dir, name)
    return candidates


def _cleanup_unlocked(root: Path, expected: str) -> Dict[str, Any]:
    """Apply one CAS-verified plan. Caller must hold ``graph_exclusive_lease``."""
    _require_managed_graph(root)
    consistency, plan = _plan_or_raise(root)
    _after_cleanup_plan_recompute(root, plan, consistency)
    _require_matching_revision(plan, expected)
    candidates = _validate_staging_deletion_set(root, plan)
    snapshots_dir = root / "snapshots"
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

    claims: List[HeldExistingStagingWriterClaim] = []
    tokens = [_token_for_name(consistency, name) for name in candidates]
    try:
        for name in candidates:
            claims.append(_claim_candidate(snapshots_dir, name))
        _after_cleanup_writer_claims(claims)
        if len(claims) != len(candidates):
            raise SnapshotStagingCleanupIntegrityError(
                "writer-lock claim count does not match deletion candidates"
            )
        for name, claim, token in zip(candidates, claims, tokens):
            _revalidate_claim(snapshots_dir, name, claim, token)
        _after_cleanup_revalidation(root, claims)
        _before_cleanup_deletion(root)

        deleted: List[str] = []
        for index, (name, claim, token) in enumerate(
            zip(candidates, claims, tokens)
        ):
            try:
                _revalidate_claim(snapshots_dir, name, claim, token)
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
                if isinstance(error, SnapshotStagingCleanupError):
                    raise
                raise SnapshotStagingCleanupIntegrityError(str(error)) from error
            try:
                _remove_claimed_staging_entry(
                    snapshots_dir, name, claim, token
                )
            except _SnapshotStagingCleanupMutationError as error:
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
                    and isinstance(error, SnapshotStagingCleanupIntegrityError)
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
    finally:
        for claim in claims:
            claim.close()


@contextmanager
def _snapshot_staging_cleanup_scope(
    graph: Path,
    expected_plan_revision: object,
    *,
    cleanup_confirmed: bool,
) -> Iterator[Dict[str, Any]]:
    """Yield one apply result while its exclusive lease remains held."""
    if not cleanup_confirmed:
        raise SnapshotStagingCleanupError(_CONFIRMATION_MESSAGE)
    expected = parse_plan_revision(expected_plan_revision)
    root = _resolve_graph_root(graph)
    _require_managed_graph(root)
    try:
        with graph_exclusive_lease(root):
            yield _cleanup_unlocked(root, expected)
    except SnapshotStagingCleanupError:
        raise
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_staging_cleanup(
    graph: Path,
    expected_plan_revision: str,
    *,
    cleanup_confirmed: bool,
) -> Dict[str, Any]:
    """Apply one CAS-verified staging cleanup plan.

    Returns the stable result object. Complete success has ``ok=true``.
    Partial mutation has ``ok=false`` and ``partial=true`` and does not
    raise; the CLI still exits 1 after emitting that result. Pre-deletion
    failures raise :class:`SnapshotStagingCleanupError` (exit 2) or
    :class:`SnapshotStagingCleanupIntegrityError` (exit 1).
    """
    with _snapshot_staging_cleanup_scope(
        graph,
        expected_plan_revision,
        cleanup_confirmed=cleanup_confirmed,
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Delete the CAS-verified snapshot-staging-cleanup-plan "
            "deletion candidates. Requires --cleanup-confirmed and "
            "--expected-plan-revision. snapshot-staging-cleanup-plan is "
            "the preview; this command has no dry-run. Accepts schema-2 "
            "plan revisions only. Never creates .publish.lock, and is "
            "not an MCP tool. Recursive deletion is not transactionally "
            "atomic."
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
        "--expected-plan-revision",
        required=True,
        help="sha256:<64 lowercase hex> from snapshot-staging-cleanup-plan",
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
        with _snapshot_staging_cleanup_scope(
            args.graph,
            args.expected_plan_revision,
            cleanup_confirmed=bool(args.cleanup_confirmed),
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
    except SnapshotStagingCleanupError as error:
        print(f"snapshot-staging-cleanup: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-staging-cleanup: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
