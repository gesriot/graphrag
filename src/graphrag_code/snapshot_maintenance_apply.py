#!/usr/bin/env python
"""Apply one CAS-verified composite snapshot maintenance plan.

``snapshot-maintenance-apply`` is the mutating counterpart of
``snapshot-maintenance-plan`` schema 1. It recomputes that composite
under one exclusive existing-lock graph lease, compares
``maintenance_revision``, and then deletes only the captured
candidates. There is no dry-run; the plan command is the preview.
Without ``--maintenance-confirmed`` the command exits 2 and changes
nothing. Confirmation is required even when both deletion sets are
empty.

The command requires an already-adopted regular ``.publish.lock`` and
never creates, truncates, rewrites, chmods, or replaces that lock. It
never creates or changes ``.snapshot-pins.json`` or ``current``. One
exclusive existing-lock lease covers plan recomputation, CAS,
preflight, mutation, result construction, serialization, stdout write,
and stdout flush. It does not take a nested graph lease and does not
call the public or scope entry points of ``snapshot-prune`` or
``snapshot-staging-cleanup``.

After writer-lock claims the ordinary cleanup plan is not recomputed:
those claims would change observed lease state. Revalidation uses the
captured staging consistency tokens. Internal execution order is
``snapshot-staging-cleanup`` then ``snapshot-prune``. That is this
command's conservative apply order, not a recommendation on the
read-only plan. Recursive deletion is not transactionally atomic.
A partial result is always emitted and always requires a fresh plan.
MCP stays exactly 14 read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-maintenance-apply --graph <root> --keep-last <N> \\
        --expected-maintenance-revision sha256:<hex> --maintenance-confirmed [--json]
    python -m graphrag_code.snapshot_maintenance_apply --graph <root> --keep-last <N> \\
        --expected-maintenance-revision sha256:<hex> --maintenance-confirmed [--json]
    uv run python scripts/snapshot_maintenance_apply.py --graph <root> --keep-last <N> \\
        --expected-maintenance-revision sha256:<hex> --maintenance-confirmed [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from graphrag_code import snapshot_prune as _prune
from graphrag_code import snapshot_staging_cleanup as _cleanup
from graphrag_code.byog_graph import (
    ByogPublicationLockError,
    ByogReaderLockError,
    _validate_managed_snapshot_layout,
    graph_exclusive_lease,
)
from graphrag_code.snapshot_maintenance_plan import (
    SnapshotMaintenancePlanError,
    SnapshotMaintenancePlanIntegrityError,
    _parse_keep_last,
    build_stable_maintenance_plan_unlocked,
)
from graphrag_code.snapshot_prune import SnapshotPruneError, SnapshotPruneIntegrityError
from graphrag_code.snapshot_retention import (
    SnapshotRetentionError,
    SnapshotRetentionIntegrityError,
    _lock_identity,
    verify_retention_plan_inputs_unlocked,
)
from graphrag_code.snapshot_staging_cleanup import (
    SnapshotStagingCleanupError,
    SnapshotStagingCleanupIntegrityError,
    _SnapshotStagingCleanupMutationError,
)

APPLY_SCHEMA_VERSION = 1
_COMPONENT_PRUNE = "snapshot-prune"
_COMPONENT_CLEANUP = "snapshot-staging-cleanup"
_APPLY_ORDER: Tuple[str, ...] = (_COMPONENT_CLEANUP, _COMPONENT_PRUNE)
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_ERROR_CHARS = 400
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_CONFIRMATION_MESSAGE = """\
refusing to apply snapshot maintenance without --maintenance-confirmed.

This is an explicit mutating CLI operation. It deletes only the
CAS-verified composite deletion candidates: staging leftovers first,
then published snapshot-prune candidates. It does not activate,
publish, change current, write the pin registry, repair, or reindex.
snapshot-maintenance-plan is the preview; this command has no dry-run
mode. Confirmation is required even when both deletion sets are empty.

--expected-maintenance-revision is a compare-and-swap guard: the
exclusive existing-lock lease recomputes the complete composite plan
and deletes only when that token still matches. A mismatched revision
changes nothing. Recursive deletion is not transactionally atomic. A
crash or later-candidate failure can leave a partially applied
maintenance run; there is no rollback. A partial result always
requires a fresh plan. Advisory locks protect only cooperating
processes.
""".strip()
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "apply_order_is_command_execution",
        "kind": "notice",
        "message": (
            "Internal execution order is snapshot-staging-cleanup then "
            "snapshot-prune. That is this command's conservative apply "
            "order, not a recommendation on snapshot-maintenance-plan. "
            "actionable_components is not an apply order."
        ),
    },
    {
        "code": "plan_observation_is_not_claim",
        "kind": "notice",
        "message": (
            "The embedded cleanup plan's not_held_at_scan observation "
            "was not this destructive claim. Apply takes fresh exclusive "
            "claims on each selected existing writer lock and does not "
            "recompute that plan after those claims."
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
            "rollback, trash, quarantine, backup, repair, or recovery."
        ),
    },
    {
        "code": "fresh_plan_required_after_any_apply",
        "kind": "notice",
        "message": (
            "A fresh composite or standalone plan is required after "
            "every apply before running another mutation. Applying "
            "either component can invalidate the other revision."
        ),
    },
    {
        "code": "cli_only_not_mcp",
        "kind": "notice",
        "message": (
            "snapshot-maintenance-apply is CLI-only and intentionally "
            "absent from the fixed 14-tool MCP set."
        ),
    },
)


class SnapshotMaintenanceApplyError(Exception):
    """Expected apply failure before mutation. Default exit 2."""

    exit_code = 2


class SnapshotMaintenanceApplyIntegrityError(SnapshotMaintenanceApplyError):
    """Mismatched maintenance revision, integrity, claim, or concurrency. Exit 1."""

    exit_code = 1


def parse_maintenance_revision(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotMaintenanceApplyError(
            "expected-maintenance-revision must be sha256:<64 lowercase hex>"
        )
    if value != value.strip():
        raise SnapshotMaintenanceApplyError(
            "expected-maintenance-revision must not contain leading or trailing "
            f"whitespace: {value!r}"
        )
    if not _REVISION_RE.fullmatch(value):
        raise SnapshotMaintenanceApplyError(
            "expected-maintenance-revision must be sha256:<64 lowercase hex>, "
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
            "snapshot-maintenance-apply: PARTIAL FAILURE "
            f"graph={result.get('graph')} "
            f"completed={','.join(result.get('completed_components') or []) or '-'} "
            f"stopped_on={result.get('stopped_on_component')} "
            f"deleted_staging={len(result.get('deleted_staging_entries') or [])} "
            f"deleted_snapshots={len(result.get('deleted_snapshots') or [])} "
            f"failed_staging={result.get('failed_staging_entry')} "
            f"failed_snapshot={result.get('failed_snapshot')} "
            "filesystem_may_have_changed=true "
            "retry_requires_fresh_plan=true "
            "There is no rollback; capture a fresh "
            "snapshot-maintenance-plan before retry."
        )
    return (
        "snapshot-maintenance-apply: "
        f"graph={result.get('graph')} "
        f"keep_last={result.get('keep_last')} "
        f"current={result.get('current')} "
        f"deleted_staging={len(result.get('deleted_staging_entries') or [])} "
        f"deleted_snapshots={len(result.get('deleted_snapshots') or [])} "
        f"observed_maintenance_revision={result.get('observed_maintenance_revision')} "
        f"observed_retention_plan_revision={result.get('observed_retention_plan_revision')} "
        "observed_staging_cleanup_plan_revision="
        f"{result.get('observed_staging_cleanup_plan_revision')} "
        f"changed={str(result.get('changed')).lower()} "
        "fresh_plan_required_after_any_apply=true "
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
        raise SnapshotMaintenanceApplyError(
            f"graph root does not exist: {path}"
        ) from error
    except OSError as error:
        raise SnapshotMaintenanceApplyError(
            f"cannot inspect graph root {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotMaintenanceApplyError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotMaintenanceApplyError(
            f"graph root is not a real directory: {path}"
        )
    return path.resolve()


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotMaintenanceApplyError(str(error)) from error
    if not managed:
        raise SnapshotMaintenanceApplyError(
            "legacy flat-parquet directory has no managed snapshot "
            f"maintenance to apply: {root}"
        )


def _lock_error(error: Exception) -> SnapshotMaintenanceApplyError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotMaintenanceApplyError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotMaintenanceApplyIntegrityError(message)
    return SnapshotMaintenanceApplyError(message)


def _bounded_error(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}"
    text = " ".join(text.split())
    if len(text) > _MAX_ERROR_CHARS:
        return text[: _MAX_ERROR_CHARS - 3] + "..."
    return text


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(dict.fromkeys(values), key=lambda item: item.encode("utf-8"))


def _wrap_component_error(error: Exception) -> SnapshotMaintenanceApplyError:
    if isinstance(error, SnapshotMaintenanceApplyError):
        return error
    if isinstance(
        error,
        (
            SnapshotMaintenancePlanIntegrityError,
            SnapshotPruneIntegrityError,
            SnapshotStagingCleanupIntegrityError,
            SnapshotRetentionIntegrityError,
        ),
    ):
        return SnapshotMaintenanceApplyIntegrityError(str(error))
    if isinstance(
        error,
        (
            SnapshotMaintenancePlanError,
            SnapshotPruneError,
            SnapshotStagingCleanupError,
            SnapshotRetentionError,
        ),
    ):
        wrapped = SnapshotMaintenanceApplyError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    return SnapshotMaintenanceApplyError(str(error))


def _after_maintenance_plan_recompute(
    root: Path, plan: Mapping[str, Any], consistency: Mapping[str, Any]
) -> None:
    """Test hook. Called after the exclusive-lease composite recompute."""
    return


def _after_maintenance_writer_claims(claims: Sequence[Any]) -> None:
    """Test hook. Called after every selected staging writer lock is claimed."""
    return


def _after_maintenance_revalidation(root: Path, claims: Sequence[Any]) -> None:
    """Test hook. Called after every staging candidate is revalidated."""
    return


def _before_maintenance_deletion(root: Path) -> None:
    """Test hook. Called after preflight and before the first unlink."""
    return


def _remaining_published(
    plan: Mapping[str, Any],
    *,
    deleted: Sequence[str],
    failed: Optional[str],
    not_attempted: Sequence[str],
) -> List[str]:
    remaining = {
        snap_id
        for snap_id in plan["published_snapshots"]
        if snap_id not in set(deleted)
    }
    if failed is not None:
        remaining.add(failed)
    remaining.update(not_attempted)
    return [
        snap_id
        for snap_id in plan["published_snapshots"]
        if snap_id in remaining
    ]


def _remaining_staging(
    cleanup: Mapping[str, Any],
    *,
    failed: Optional[str],
    not_attempted: Sequence[str],
) -> List[str]:
    remaining = [
        str(item.get("name") or "")
        for item in (cleanup.get("blocked_entries") or [])
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
    deleted_snapshots: Sequence[str],
    deleted_staging: Sequence[str],
    failed_snapshot: Optional[str],
    failed_staging_entry: Optional[str],
    not_attempted_snapshots: Sequence[str],
    not_attempted_staging: Sequence[str],
    completed_components: Sequence[str],
    stopped_on_component: Optional[str],
    not_attempted_components: Sequence[str],
    error: Optional[str],
    partial: bool,
) -> Dict[str, Any]:
    retention = plan["retention_plan"]
    cleanup = plan["staging_cleanup_plan"]
    deleted_snap_ids = list(deleted_snapshots)
    deleted_stage_ids = list(deleted_staging)
    return {
        "schema_version": APPLY_SCHEMA_VERSION,
        "ok": not partial,
        "changed": bool(deleted_snap_ids or deleted_stage_ids),
        "partial": partial,
        "filesystem_may_have_changed": bool(deleted_snap_ids or deleted_stage_ids)
        or partial,
        "retry_requires_fresh_plan": partial,
        "fresh_plan_required_after_any_apply": True,
        "graph": plan["graph"],
        "keep_last": plan["keep_last"],
        "expected_maintenance_revision": expected,
        "observed_maintenance_revision": plan["maintenance_revision"],
        "observed_retention_plan_revision": retention["plan_revision"],
        "observed_staging_cleanup_plan_revision": cleanup["plan_revision"],
        "current": plan["current"],
        "published_snapshots": list(plan["published_snapshots"]),
        "maintenance_confirmed": True,
        "actionable_components": list(plan["actionable_components"]),
        "component_apply_order": list(_APPLY_ORDER),
        "completed_components": list(completed_components),
        "stopped_on_component": stopped_on_component,
        "not_attempted_components": list(not_attempted_components),
        "planned_deletion_snapshots": list(retention["deletion_candidates"]),
        "planned_deletion_staging_entries": list(cleanup["deletion_candidates"]),
        "deleted_snapshots": deleted_snap_ids,
        "deleted_staging_entries": deleted_stage_ids,
        "failed_snapshot": failed_snapshot,
        "failed_staging_entry": failed_staging_entry,
        "not_attempted_snapshots": list(not_attempted_snapshots),
        "not_attempted_staging_entries": list(not_attempted_staging),
        "remaining_published_snapshots": _remaining_published(
            plan,
            deleted=deleted_snap_ids,
            failed=failed_snapshot,
            not_attempted=not_attempted_snapshots,
        ),
        "remaining_staging_entries": _remaining_staging(
            cleanup,
            failed=failed_staging_entry,
            not_attempted=not_attempted_staging,
        ),
        "error": error,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }


def _revalidate_retention_inputs(
    root: Path,
    retention: Mapping[str, Any],
    *,
    lock_identity: Tuple[int, int, int, int],
    check_staging_notices: bool = True,
) -> None:
    try:
        verify_retention_plan_inputs_unlocked(
            root,
            retention,
            lock_identity=lock_identity,
            check_staging_notices=check_staging_notices,
        )
    except SnapshotRetentionError as error:
        raise _wrap_component_error(error) from error


def _apply_unlocked(root: Path, keep_last: int, expected: str) -> Dict[str, Any]:
    """Apply one CAS-verified composite. Caller holds ``graph_exclusive_lease``."""
    _require_managed_graph(root)
    try:
        # Capture the publication-lock pathname identity before constructing
        # the plan.  Taking a new baseline after the plan would let a
        # lock-ignoring replacement in that interval become the accepted
        # identity even though this process still holds the old descriptor.
        lock_identity = _lock_identity(root)
        consistency, plan = build_stable_maintenance_plan_unlocked(root, keep_last)
    except SnapshotMaintenancePlanError as error:
        raise _wrap_component_error(error) from error
    except SnapshotRetentionError as error:
        raise _wrap_component_error(error) from error
    _after_maintenance_plan_recompute(root, plan, consistency)
    observed = plan.get("maintenance_revision")
    if observed != expected:
        raise SnapshotMaintenanceApplyIntegrityError(
            f"expected-maintenance-revision {expected!r} does not match "
            f"observed {observed!r}; refusing to delete"
        )
    retention = plan["retention_plan"]
    cleanup = plan["staging_cleanup_plan"]
    try:
        prune_candidates = _prune._validate_deletion_set(root, retention)
        cleanup_candidates = _cleanup._validate_staging_deletion_set(root, cleanup)
    except (SnapshotPruneError, SnapshotStagingCleanupError) as error:
        raise _wrap_component_error(error) from error
    if prune_candidates != list(retention["deletion_candidates"]):
        raise SnapshotMaintenanceApplyIntegrityError(
            "published-snapshot deletion set changed before mutation"
        )
    if cleanup_candidates != list(cleanup["deletion_candidates"]):
        raise SnapshotMaintenanceApplyIntegrityError(
            "staging deletion set changed before mutation"
        )
    _revalidate_retention_inputs(
        root,
        retention,
        lock_identity=lock_identity,
    )

    snapshots_dir = root / "snapshots"
    claims: List[Any] = []
    tokens = [
        _cleanup._token_for_name(consistency, name) for name in cleanup_candidates
    ]
    try:
        for name in cleanup_candidates:
            try:
                claims.append(_cleanup._claim_candidate(snapshots_dir, name))
            except SnapshotStagingCleanupError as error:
                raise _wrap_component_error(error) from error
        _after_maintenance_writer_claims(claims)
        if len(claims) != len(cleanup_candidates):
            raise SnapshotMaintenanceApplyIntegrityError(
                "writer-lock claim count does not match deletion candidates"
            )
        for name, claim, token in zip(cleanup_candidates, claims, tokens):
            try:
                _cleanup._revalidate_claim(snapshots_dir, name, claim, token)
            except SnapshotStagingCleanupError as error:
                raise _wrap_component_error(error) from error
        _after_maintenance_revalidation(root, claims)
        _revalidate_retention_inputs(
            root,
            retention,
            lock_identity=lock_identity,
        )
        _before_maintenance_deletion(root)

        deleted_staging: List[str] = []
        deleted_snapshots: List[str] = []
        completed: List[str] = []

        if cleanup_candidates:
            for index, (name, claim, token) in enumerate(
                zip(cleanup_candidates, claims, tokens)
            ):
                try:
                    _cleanup._revalidate_claim(snapshots_dir, name, claim, token)
                except Exception as error:
                    if deleted_staging:
                        return _result(
                            plan,
                            expected=expected,
                            deleted_snapshots=[],
                            deleted_staging=deleted_staging,
                            failed_snapshot=None,
                            failed_staging_entry=name,
                            not_attempted_snapshots=prune_candidates,
                            not_attempted_staging=cleanup_candidates[index + 1 :],
                            completed_components=completed,
                            stopped_on_component=_COMPONENT_CLEANUP,
                            not_attempted_components=(
                                [_COMPONENT_PRUNE] if prune_candidates else []
                            ),
                            error=_bounded_error(error),
                            partial=True,
                        )
                    raise _wrap_component_error(error) from error
                try:
                    _cleanup._remove_claimed_staging_entry(
                        snapshots_dir, name, claim, token
                    )
                except _SnapshotStagingCleanupMutationError as error:
                    return _result(
                        plan,
                        expected=expected,
                        deleted_snapshots=[],
                        deleted_staging=deleted_staging,
                        failed_snapshot=None,
                        failed_staging_entry=name,
                        not_attempted_snapshots=prune_candidates,
                        not_attempted_staging=cleanup_candidates[index + 1 :],
                        completed_components=completed,
                        stopped_on_component=_COMPONENT_CLEANUP,
                        not_attempted_components=(
                            [_COMPONENT_PRUNE] if prune_candidates else []
                        ),
                        error=_bounded_error(error.cause),
                        partial=True,
                    )
                except Exception as error:
                    if not deleted_staging and isinstance(
                        error, SnapshotStagingCleanupIntegrityError
                    ):
                        raise _wrap_component_error(error) from error
                    return _result(
                        plan,
                        expected=expected,
                        deleted_snapshots=[],
                        deleted_staging=deleted_staging,
                        failed_snapshot=None,
                        failed_staging_entry=name,
                        not_attempted_snapshots=prune_candidates,
                        not_attempted_staging=cleanup_candidates[index + 1 :],
                        completed_components=completed,
                        stopped_on_component=_COMPONENT_CLEANUP,
                        not_attempted_components=(
                            [_COMPONENT_PRUNE] if prune_candidates else []
                        ),
                        error=_bounded_error(error),
                        partial=True,
                    )
                deleted_staging.append(name)
            completed.append(_COMPONENT_CLEANUP)

        if prune_candidates:
            try:
                # Successful staging cleanup deliberately changes only the
                # retention planner's presentation-only staging notices.
                # Compare every destructive retention input against the
                # original CAS-verified plan before the first published-
                # snapshot unlink.
                _revalidate_retention_inputs(
                    root,
                    retention,
                    lock_identity=lock_identity,
                    check_staging_notices=False,
                )
                revalidated_prune_candidates = _prune._validate_deletion_set(
                    root, retention
                )
                if revalidated_prune_candidates != prune_candidates:
                    raise SnapshotMaintenanceApplyIntegrityError(
                        "published-snapshot deletion set changed before prune"
                    )
            except Exception as error:
                if deleted_staging:
                    return _result(
                        plan,
                        expected=expected,
                        deleted_snapshots=[],
                        deleted_staging=deleted_staging,
                        failed_snapshot=None,
                        failed_staging_entry=None,
                        not_attempted_snapshots=prune_candidates,
                        not_attempted_staging=[],
                        completed_components=completed,
                        stopped_on_component=_COMPONENT_PRUNE,
                        not_attempted_components=[],
                        error=_bounded_error(error),
                        partial=True,
                    )
                raise _wrap_component_error(error) from error
            for index, snap_id in enumerate(prune_candidates):
                try:
                    _prune._remove_published_snapshot_directory(snapshots_dir, snap_id)
                except Exception as error:
                    return _result(
                        plan,
                        expected=expected,
                        deleted_snapshots=deleted_snapshots,
                        deleted_staging=deleted_staging,
                        failed_snapshot=snap_id,
                        failed_staging_entry=None,
                        not_attempted_snapshots=prune_candidates[index + 1 :],
                        not_attempted_staging=[],
                        completed_components=completed,
                        stopped_on_component=_COMPONENT_PRUNE,
                        not_attempted_components=[],
                        error=_bounded_error(error),
                        partial=True,
                    )
                deleted_snapshots.append(snap_id)
            completed.append(_COMPONENT_PRUNE)

        return _result(
            plan,
            expected=expected,
            deleted_snapshots=deleted_snapshots,
            deleted_staging=deleted_staging,
            failed_snapshot=None,
            failed_staging_entry=None,
            not_attempted_snapshots=[],
            not_attempted_staging=[],
            completed_components=completed,
            stopped_on_component=None,
            not_attempted_components=[],
            error=None,
            partial=False,
        )
    finally:
        for claim in claims:
            claim.close()


@contextmanager
def _snapshot_maintenance_apply_scope(
    graph: Path,
    keep_last: object,
    expected_maintenance_revision: object,
    *,
    maintenance_confirmed: bool,
) -> Iterator[Dict[str, Any]]:
    """Yield one apply result while its exclusive lease remains held."""
    if not maintenance_confirmed:
        raise SnapshotMaintenanceApplyError(_CONFIRMATION_MESSAGE)
    try:
        requested = _parse_keep_last(keep_last)
    except SnapshotMaintenancePlanError as error:
        raise SnapshotMaintenanceApplyError(str(error)) from error
    expected = parse_maintenance_revision(expected_maintenance_revision)
    root = _resolve_graph_root(graph)
    _require_managed_graph(root)
    try:
        with graph_exclusive_lease(root):
            yield _apply_unlocked(root, requested, expected)
    except SnapshotMaintenanceApplyError:
        raise
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_maintenance_apply(
    graph: Path,
    keep_last: int,
    expected_maintenance_revision: str,
    *,
    maintenance_confirmed: bool,
) -> Dict[str, Any]:
    """Apply one CAS-verified composite maintenance plan.

    Returns the stable result object. Complete success has ``ok=true``.
    Partial mutation has ``ok=false`` and ``partial=true`` and does not
    raise; the CLI still exits 1 after emitting that result. Pre-deletion
    failures raise :class:`SnapshotMaintenanceApplyError` (exit 2) or
    :class:`SnapshotMaintenanceApplyIntegrityError` (exit 1).
    """
    with _snapshot_maintenance_apply_scope(
        graph,
        keep_last,
        expected_maintenance_revision,
        maintenance_confirmed=maintenance_confirmed,
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Delete the CAS-verified snapshot-maintenance-plan candidates. "
            "Requires --maintenance-confirmed and "
            "--expected-maintenance-revision. snapshot-maintenance-plan is "
            "the preview; this command has no dry-run. Applies staging "
            "cleanup then prune. Never creates .publish.lock, and is not "
            "an MCP tool. Recursive deletion is not transactionally atomic."
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
        "--keep-last",
        type=int,
        required=True,
        help="Requested keep-last floor (positive integer).",
    )
    parser.add_argument(
        "--expected-maintenance-revision",
        required=True,
        help="sha256:<64 lowercase hex> from snapshot-maintenance-plan",
    )
    parser.add_argument(
        "--maintenance-confirmed",
        action="store_true",
        help=(
            "Required to delete CAS-verified composite deletion-candidate "
            "directories. The command still refuses to delete if the "
            "recomputed maintenance_revision no longer matches."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_maintenance_apply_scope(
            args.graph,
            args.keep_last,
            args.expected_maintenance_revision,
            maintenance_confirmed=bool(args.maintenance_confirmed),
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
    except SnapshotMaintenanceApplyError as error:
        print(f"snapshot-maintenance-apply: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-maintenance-apply: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
