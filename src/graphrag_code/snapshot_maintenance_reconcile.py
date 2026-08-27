#!/usr/bin/env python
"""Read-only post-apply snapshot maintenance reconciliation.

``snapshot-maintenance-reconcile`` inspects the aftermath of a complete,
partial, interrupted, or externally modified maintenance run. It is
observation-only. It does not recover, roll back, delete, repair, or
claim that an absent path was deleted by ``snapshot-maintenance-apply``.

The command requires a saved schema-1 ``snapshot-maintenance-plan``
file. An optional saved schema-1 ``snapshot-maintenance-apply`` result
may be supplied for conservative comparison. Both paths are relative to
the invoking cwd unless absolute. Only bounded regular files are
accepted; they are opened read-only without following symlinks. The
conservative limit is ``MAX_INPUT_BYTES`` (1 MiB).

Input loading, structural validation, and plan/result cross-validation
all precede graph inspection. Malformed, oversized, symlinked, replaced,
or structurally invalid input files fail with exit 2 and empty stdout.
Plan validation recomputes the composite and both embedded component
self-hashes and rejects non-direct candidate names. A structurally valid
apply result that refers to another plan is an integrity failure: exit 1
and empty stdout.

The command requires a managed ``current + snapshots/`` graph and an
already-adopted regular ``.publish.lock``. It never creates, truncates,
rewrites, chmods, or replaces that lock. One shared existing-lock lease
covers graph inspection, result construction, serialization, stdout
write, and stdout flush. It does not take a nested graph lease and does
not call publishers, extractors, repair/reindex, ``snapshot-prune``,
``snapshot-staging-cleanup``, or ``snapshot-maintenance-apply``.

Writer-lock observation reuses the read-only staging inventory probe.
It never claims or creates writer locks and does not infer writer
death, ownership, age, PID, host, or safety to delete. A new
maintenance plan is still required before any later mutation. MCP stays
exactly 14 read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-maintenance-reconcile --graph <root> \\
        --plan-file <plan.json> [--apply-result-file <result.json>] [--json]
    python -m graphrag_code.snapshot_maintenance_reconcile --graph <root> \\
        --plan-file <plan.json> [--apply-result-file <result.json>] [--json]
    uv run python scripts/snapshot_maintenance_reconcile.py --graph <root> \\
        --plan-file <plan.json> [--apply-result-file <result.json>] [--json]
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
from graphrag_code.snapshot_maintenance_plan import (
    SnapshotMaintenancePlanError,
    maintenance_revision_of,
)
from graphrag_code.snapshot_retention import (
    SnapshotRetentionError,
    plan_revision_of as retention_plan_revision_of,
)
from graphrag_code.snapshot_staging_cleanup_plan import (
    SnapshotStagingCleanupPlanError,
    plan_revision_of as staging_cleanup_plan_revision_of,
)
from graphrag_code.snapshot_staging import (
    MAX_PUBLISHED_SNAPSHOTS,
    MAX_STAGING_ENTRIES,
    SnapshotStagingError,
    SnapshotStagingIntegrityError,
    _entry_kind,
    _lock_identity,
    _read_current,
    _safe_directory_entries,
)

RECONCILE_SCHEMA_VERSION = 1
PLAN_SCHEMA_VERSION = 1
APPLY_SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1 * 1024 * 1024
_COMPONENT_PRUNE = "snapshot-prune"
_COMPONENT_CLEANUP = "snapshot-staging-cleanup"
_APPLY_ORDER: Tuple[str, ...] = (_COMPONENT_CLEANUP, _COMPONENT_PRUNE)
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_STATE_ABSENT = "absent_at_reconcile"
_STATE_DIRECTORY = "present_directory_at_reconcile"
_STATE_NON_DIRECTORY = "present_non_directory_at_reconcile"
_STATE_SYMLINK = "unsafe_symlink_at_reconcile"
_STATE_CHANGED = "changed_during_reconcile"
_DECLARED_DELETED = "declared_completely_deleted"
_DECLARED_FAILED = "failed"
_DECLARED_NOT_ATTEMPTED = "not_attempted"
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "reconciliation_is_observation_only",
        "kind": "notice",
        "message": (
            "snapshot-maintenance-reconcile is observation-only. It does "
            "not recover, roll back, delete, quarantine, repair, or "
            "recommend an apply. ok means this read completed, not that "
            "maintenance completed successfully."
        ),
    },
    {
        "code": "deletion_cause_not_proven",
        "kind": "notice",
        "message": (
            "An absent pathname is not proof that snapshot-maintenance-apply "
            "deleted it. deletion_cause_proven is always false. "
            "Replacements cannot be proven identical from the saved public "
            "plan or apply result."
        ),
    },
    {
        "code": "no_recovery_performed",
        "kind": "notice",
        "message": (
            "recovery_performed is always false. This command does not "
            "create, truncate, chmod, rewrite, or replace any file, "
            "including .publish.lock, current, snapshots, staging, writer "
            "locks, .snapshot-pins.json, and the input files."
        ),
    },
    {
        "code": "fresh_plan_required_before_mutation",
        "kind": "notice",
        "message": (
            "A new snapshot-maintenance-plan is still required before any "
            "later mutation. This reconciliation is not a retry token and "
            "does not accept maintenance_revision for apply."
        ),
    },
    {
        "code": "advisory_locks_cooperating_only",
        "kind": "notice",
        "message": (
            "Advisory locks protect only cooperating processes. Writer-lock "
            "observation reuses the read-only staging inventory probe and "
            "does not infer writer death, ownership, age, PID, host, or "
            "safety to delete."
        ),
    },
    {
        "code": "input_files_bounded",
        "kind": "notice",
        "message": (
            "Saved plan and apply-result files must be regular files no "
            f"larger than {MAX_INPUT_BYTES} bytes. They are opened "
            "read-only without following symlinks."
        ),
    },
    {
        "code": "cli_only_not_mcp",
        "kind": "notice",
        "message": (
            "snapshot-maintenance-reconcile is CLI-only and intentionally "
            "absent from the fixed 14-tool MCP set."
        ),
    },
)


class SnapshotMaintenanceReconcileError(Exception):
    """Expected reconcile failure before a result is emitted. Default exit 2."""

    exit_code = 2


class SnapshotMaintenanceReconcileIntegrityError(SnapshotMaintenanceReconcileError):
    """Unsafe graph structure, concurrent mutation, or result/plan mismatch. Exit 1."""

    exit_code = 1


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
    consistent = result.get("result_consistent_with_observation")
    if consistent is None:
        consistent_text = "null"
    else:
        consistent_text = str(bool(consistent)).lower()
    return (
        "snapshot-maintenance-reconcile: "
        f"graph={result.get('graph')} "
        f"current={result.get('observed_current')} "
        f"published={len(result.get('observed_published_snapshots') or [])} "
        f"input_plan_revision={result.get('input_plan_revision')} "
        "apply_result_supplied="
        f"{str(result.get('apply_result_supplied')).lower()} "
        "all_planned_candidates_absent_at_reconcile="
        f"{str(result.get('all_planned_candidates_absent_at_reconcile')).lower()} "
        f"result_consistent_with_observation={consistent_text} "
        f"discrepancies={len(result.get('discrepancies') or [])} "
        "reconciliation_is_observation_only=true "
        "deletion_cause_proven=false"
    )


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotMaintenanceReconcileError(
            f"graph root does not exist: {path}"
        ) from error
    except OSError as error:
        raise SnapshotMaintenanceReconcileError(
            f"cannot inspect graph root {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotMaintenanceReconcileError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotMaintenanceReconcileError(
            f"graph root is not a real directory: {path}"
        )
    return path.resolve()


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotMaintenanceReconcileError(str(error)) from error
    if not managed:
        raise SnapshotMaintenanceReconcileError(
            "legacy flat-parquet directory has no managed snapshot "
            f"maintenance to reconcile: {root}"
        )


def _lock_error(error: Exception) -> SnapshotMaintenanceReconcileError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotMaintenanceReconcileError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotMaintenanceReconcileIntegrityError(message)
    return SnapshotMaintenanceReconcileError(message)


def _wrap_staging_error(error: Exception) -> SnapshotMaintenanceReconcileError:
    if isinstance(error, SnapshotStagingIntegrityError):
        return SnapshotMaintenanceReconcileIntegrityError(str(error))
    if isinstance(error, SnapshotStagingError):
        wrapped = SnapshotMaintenanceReconcileError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    return SnapshotMaintenanceReconcileError(str(error))


def _require_revision(value: object, label: str) -> str:
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise SnapshotMaintenanceReconcileError(
            f"{label} must be sha256:<64 lowercase hex>, got {value!r}"
        )
    return value


def _require_string_list(value: object, label: str) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SnapshotMaintenanceReconcileError(f"{label} must be an array of strings")
    return list(value)


def _require_optional_string(value: object, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise SnapshotMaintenanceReconcileError(
            f"{label} must be a non-empty string or null"
        )
    return value


def _actionable_from_embedded(
    retention: Mapping[str, Any], cleanup: Mapping[str, Any]
) -> List[str]:
    names: List[str] = []
    if retention.get("deletion_candidates"):
        names.append(_COMPONENT_PRUNE)
    if cleanup.get("deletion_candidates"):
        names.append(_COMPONENT_CLEANUP)
    return _byte_sort(names)


def _after_input_path_lstat(_path: Path) -> None:
    """Test hook. Called after lstat and before the no-follow open."""
    return None


def _after_first_reconcile_scan(_root: Path, _scan: Mapping[str, Any]) -> None:
    """Test hook. Called after the first graph scan, before the second."""
    return None


def _read_bounded_regular_file(path: Path, *, label: str) -> bytes:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    try:
        before = resolved.lstat()
    except FileNotFoundError as error:
        raise SnapshotMaintenanceReconcileError(
            f"{label} does not exist: {resolved}"
        ) from error
    except OSError as error:
        raise SnapshotMaintenanceReconcileError(
            f"cannot inspect {label} {resolved}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotMaintenanceReconcileError(
            f"{label} must be a regular file, not a symlink: {resolved}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotMaintenanceReconcileError(
            f"{label} is not a regular file: {resolved}"
        )
    if before.st_size > MAX_INPUT_BYTES:
        raise SnapshotMaintenanceReconcileError(
            f"{label} exceeds bound {MAX_INPUT_BYTES} bytes: {resolved}"
        )
    if not hasattr(os, "O_NOFOLLOW"):
        raise SnapshotMaintenanceReconcileError(
            "safe no-follow input-file reads are unsupported on "
            f"this platform: {sys.platform!r}"
        )
    _after_input_path_lstat(resolved)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(resolved), flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT}:
            raise SnapshotMaintenanceReconcileError(
                f"{label} changed or became unsafe while opening it: {resolved}"
            ) from error
        raise SnapshotMaintenanceReconcileError(
            f"cannot safely open {label} {resolved}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = resolved.lstat()
        except OSError as error:
            raise SnapshotMaintenanceReconcileError(
                f"{label} changed while opening it: {resolved}"
            ) from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise SnapshotMaintenanceReconcileError(
                f"{label} changed or became unsafe while opening it: {resolved}"
            )
        chunks: List[bytes] = []
        total = 0
        while total <= MAX_INPUT_BYTES:
            chunk = os.read(fd, min(8192, MAX_INPUT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        if len(data) > MAX_INPUT_BYTES:
            raise SnapshotMaintenanceReconcileError(
                f"{label} exceeds bound {MAX_INPUT_BYTES} bytes: {resolved}"
            )
        after_fd = os.fstat(fd)
        try:
            after_path = resolved.lstat()
        except OSError as error:
            raise SnapshotMaintenanceReconcileError(
                f"{label} changed while it was read: {resolved}"
            ) from error
        identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        if (
            (after_fd.st_dev, after_fd.st_ino, after_fd.st_size, after_fd.st_mtime_ns)
            != identity
            or stat.S_ISLNK(after_path.st_mode)
            or not stat.S_ISREG(after_path.st_mode)
            or (
                after_path.st_dev,
                after_path.st_ino,
                after_path.st_size,
                after_path.st_mtime_ns,
            )
            != identity
            or len(data) != opened.st_size
        ):
            raise SnapshotMaintenanceReconcileError(
                f"{label} changed while it was read: {resolved}"
            )
    finally:
        os.close(fd)
    return data


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"non-standard numeric constant {value!r}")


def _load_json_object(path: Path, *, label: str) -> Dict[str, Any]:
    data = _read_bounded_regular_file(path, label=label)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SnapshotMaintenanceReconcileError(
            f"{label} is not valid UTF-8: {path}"
        ) from error
    try:
        parsed = json.loads(text, parse_constant=_reject_nonstandard_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise SnapshotMaintenanceReconcileError(
            f"{label} is not valid JSON: {error}"
        ) from error
    if not isinstance(parsed, dict):
        raise SnapshotMaintenanceReconcileError(f"{label} must be a JSON object")
    return parsed


def _require_canonical_name_list(
    value: object,
    label: str,
    *,
    kind: str,
    maximum: int,
) -> List[str]:
    names = _require_string_list(value, label)
    if len(names) > maximum:
        raise SnapshotMaintenanceReconcileError(
            f"{label} exceeds entry bound {maximum}"
        )
    try:
        for name in names:
            name.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SnapshotMaintenanceReconcileError(
            f"{label} contains a string that is not valid UTF-8"
        ) from error
    if len(set(names)) != len(names) or names != _byte_sort(names):
        raise SnapshotMaintenanceReconcileError(
            f"{label} must be unique and sorted in UTF-8-byte order"
        )
    if kind == "published":
        invalid = [name for name in names if not is_published_snapshot_id(name)]
    elif kind == "staging":
        invalid = [
            name
            for name in names
            if not (
                is_staging_snapshot_name(name)
                and name not in {".", ".."}
                and "/" not in name
                and "\\" not in name
                and "\x00" not in name
                and Path(name).name == name
            )
        ]
    else:  # pragma: no cover - internal programming error
        raise AssertionError(f"unknown candidate kind: {kind}")
    if invalid:
        raise SnapshotMaintenanceReconcileError(
            f"{label} contains a non-canonical direct {kind} name: {invalid[0]!r}"
        )
    return names


def _validate_saved_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    if plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise SnapshotMaintenanceReconcileError(
            "saved plan schema_version must be 1"
        )
    if plan.get("ok") is not True:
        raise SnapshotMaintenanceReconcileError("saved plan ok must be true")
    keep_last = plan.get("keep_last")
    if isinstance(keep_last, bool) or not isinstance(keep_last, int) or keep_last < 1:
        raise SnapshotMaintenanceReconcileError(
            "saved plan keep_last must be a positive integer"
        )
    graph = plan.get("graph")
    if not isinstance(graph, str) or not graph or not Path(graph).is_absolute():
        raise SnapshotMaintenanceReconcileError(
            "saved plan graph must be a non-empty absolute path"
        )
    current = plan.get("current")
    if not isinstance(current, str) or not is_published_snapshot_id(current):
        raise SnapshotMaintenanceReconcileError(
            "saved plan current must be a canonical published snapshot id"
        )
    published = _require_canonical_name_list(
        plan.get("published_snapshots"),
        "saved plan published_snapshots",
        kind="published",
        maximum=MAX_PUBLISHED_SNAPSHOTS,
    )
    if current not in published:
        raise SnapshotMaintenanceReconcileError(
            "saved plan current must be present in published_snapshots"
        )
    retention = plan.get("retention_plan")
    cleanup = plan.get("staging_cleanup_plan")
    if not isinstance(retention, dict) or not isinstance(cleanup, dict):
        raise SnapshotMaintenanceReconcileError(
            "saved plan is missing an embedded retention or staging-cleanup plan"
        )
    if retention.get("schema_version") != 1:
        raise SnapshotMaintenanceReconcileError(
            "saved embedded retention plan schema_version must be 1"
        )
    if cleanup.get("schema_version") != 2:
        raise SnapshotMaintenanceReconcileError(
            "saved embedded staging-cleanup plan schema_version must be 2"
        )
    if retention.get("graph") != graph or cleanup.get("graph") != graph:
        raise SnapshotMaintenanceReconcileError(
            "saved plan graph does not agree with both embedded plans"
        )
    if retention.get("keep_last_requested") != keep_last:
        raise SnapshotMaintenanceReconcileError(
            "saved plan keep_last does not agree with embedded retention plan"
        )
    if (
        cleanup.get("cleanup_applied") is not False
        or cleanup.get("apply_supported") is not True
        or cleanup.get("ownership_inference") is not False
    ):
        raise SnapshotMaintenanceReconcileError(
            "saved embedded staging-cleanup plan has invalid safety flags"
        )
    if retention.get("current") != current or cleanup.get("current") != current:
        raise SnapshotMaintenanceReconcileError(
            "saved plan current does not agree with both embedded plans"
        )
    if list(retention.get("published_snapshots") or []) != published or list(
        cleanup.get("published_snapshots") or []
    ) != published:
        raise SnapshotMaintenanceReconcileError(
            "saved plan published_snapshots does not agree with both embedded plans"
        )
    retention_revision = _require_revision(
        retention.get("plan_revision"), "saved retention plan_revision"
    )
    cleanup_revision = _require_revision(
        cleanup.get("plan_revision"), "saved staging-cleanup plan_revision"
    )
    retention_candidates = _require_canonical_name_list(
        retention.get("deletion_candidates"),
        "saved retention deletion_candidates",
        kind="published",
        maximum=MAX_PUBLISHED_SNAPSHOTS,
    )
    cleanup_candidates = _require_canonical_name_list(
        cleanup.get("deletion_candidates"),
        "saved staging-cleanup deletion_candidates",
        kind="staging",
        maximum=MAX_STAGING_ENTRIES,
    )
    blocked_entries = cleanup.get("blocked_entries")
    if not isinstance(blocked_entries, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("name"), str)
        or not item.get("name")
        or not isinstance(item.get("reason"), str)
        or not item.get("reason")
        for item in blocked_entries
    ):
        raise SnapshotMaintenanceReconcileError(
            "saved staging-cleanup blocked_entries must contain name/reason objects"
        )
    blocked_names = _require_canonical_name_list(
        [str(item["name"]) for item in blocked_entries],
        "saved staging-cleanup blocked entry names",
        kind="staging",
        maximum=MAX_STAGING_ENTRIES,
    )
    if set(blocked_names) & set(cleanup_candidates):
        raise SnapshotMaintenanceReconcileError(
            "saved staging-cleanup blocked entries overlap deletion_candidates"
        )
    if current in retention_candidates or not set(retention_candidates).issubset(
        published
    ):
        raise SnapshotMaintenanceReconcileError(
            "saved retention deletion_candidates must be non-current members "
            "of published_snapshots"
        )
    try:
        recomputed_retention = retention_plan_revision_of(retention)
        recomputed_cleanup = staging_cleanup_plan_revision_of(cleanup)
    except (
        SnapshotRetentionError,
        SnapshotStagingCleanupPlanError,
        TypeError,
        ValueError,
    ) as error:
        raise SnapshotMaintenanceReconcileError(str(error)) from error
    if recomputed_retention != retention_revision:
        raise SnapshotMaintenanceReconcileError(
            "saved retention plan_revision does not match its recomputed "
            "decision-input hash"
        )
    if recomputed_cleanup != cleanup_revision:
        raise SnapshotMaintenanceReconcileError(
            "saved staging-cleanup plan_revision does not match its recomputed "
            "decision-input hash"
        )
    actionable = _require_string_list(
        plan.get("actionable_components"), "saved plan actionable_components"
    )
    expected = _actionable_from_embedded(retention, cleanup)
    if actionable != expected:
        raise SnapshotMaintenanceReconcileError(
            "saved plan actionable_components does not match the embedded "
            f"deletion sets in UTF-8-byte order: {actionable!r} != {expected!r}"
        )
    if plan.get("fresh_plan_required_after_any_apply") is not True:
        raise SnapshotMaintenanceReconcileError(
            "saved plan fresh_plan_required_after_any_apply must be true"
        )
    saved_revision = _require_revision(
        plan.get("maintenance_revision"), "saved plan maintenance_revision"
    )
    try:
        recomputed = maintenance_revision_of(plan)
    except (SnapshotMaintenancePlanError, TypeError, ValueError) as error:
        raise SnapshotMaintenanceReconcileError(str(error)) from error
    if recomputed != saved_revision:
        raise SnapshotMaintenanceReconcileError(
            "saved plan maintenance_revision does not match the recomputed "
            f"decision-input hash: {saved_revision!r} != {recomputed!r}"
        )
    return dict(plan)


def _require_saved_plan_graph(plan: Mapping[str, Any], root: Path) -> None:
    if plan.get("graph") != str(root):
        raise SnapshotMaintenanceReconcileError(
            f"saved plan graph {plan.get('graph')!r} does not match requested "
            f"graph {str(root)!r}"
        )


def _validate_component_outcome(
    planned: Sequence[str],
    deleted: Sequence[str],
    failed: Optional[str],
    not_attempted: Sequence[str],
    *,
    component: str,
    status: str,
) -> None:
    planned_list = list(planned)
    deleted_list = list(deleted)
    not_attempted_list = list(not_attempted)
    if status == "completed":
        valid = (
            deleted_list == planned_list
            and failed is None
            and not not_attempted_list
        )
    elif status == "not_attempted":
        valid = (
            not deleted_list
            and failed is None
            and not_attempted_list == planned_list
        )
    elif status == "stopped":
        index = len(deleted_list)
        valid = deleted_list == planned_list[:index] and index < len(planned_list)
        if failed is None:
            valid = valid and not_attempted_list == planned_list[index:]
        else:
            valid = (
                valid
                and failed == planned_list[index]
                and not_attempted_list == planned_list[index + 1 :]
            )
    else:  # pragma: no cover - internal programming error
        raise AssertionError(f"unknown component outcome status: {status}")
    if not valid:
        raise SnapshotMaintenanceReconcileError(
            f"saved apply result {component} candidate outcome is not an exact "
            f"ordered {status} partition of its planned deletion set"
        )


def _validate_apply_result_structure(result: Mapping[str, Any]) -> Dict[str, Any]:
    if result.get("schema_version") != APPLY_SCHEMA_VERSION:
        raise SnapshotMaintenanceReconcileError(
            "saved apply result schema_version must be 1"
        )
    required = (
        "ok",
        "changed",
        "partial",
        "filesystem_may_have_changed",
        "retry_requires_fresh_plan",
        "fresh_plan_required_after_any_apply",
        "graph",
        "keep_last",
        "current",
        "published_snapshots",
        "maintenance_confirmed",
        "actionable_components",
        "component_apply_order",
        "expected_maintenance_revision",
        "observed_maintenance_revision",
        "observed_retention_plan_revision",
        "observed_staging_cleanup_plan_revision",
        "planned_deletion_snapshots",
        "planned_deletion_staging_entries",
        "deleted_snapshots",
        "deleted_staging_entries",
        "failed_snapshot",
        "failed_staging_entry",
        "not_attempted_snapshots",
        "not_attempted_staging_entries",
        "completed_components",
        "stopped_on_component",
        "not_attempted_components",
        "remaining_published_snapshots",
        "remaining_staging_entries",
        "error",
    )
    for key in required:
        if key not in result:
            raise SnapshotMaintenanceReconcileError(
                f"saved apply result is missing {key}"
            )
    if result.get("fresh_plan_required_after_any_apply") is not True:
        raise SnapshotMaintenanceReconcileError(
            "saved apply result fresh_plan_required_after_any_apply must be true"
        )
    keep_last = result.get("keep_last")
    if isinstance(keep_last, bool) or not isinstance(keep_last, int) or keep_last < 1:
        raise SnapshotMaintenanceReconcileError(
            "saved apply result keep_last must be a positive integer"
        )
    graph = result.get("graph")
    if not isinstance(graph, str) or not graph or not Path(graph).is_absolute():
        raise SnapshotMaintenanceReconcileError(
            "saved apply result graph must be a non-empty absolute path"
        )
    current = result.get("current")
    if not isinstance(current, str) or not is_published_snapshot_id(current):
        raise SnapshotMaintenanceReconcileError(
            "saved apply result current must be a canonical published snapshot id"
        )
    published = _require_canonical_name_list(
        result.get("published_snapshots"),
        "saved apply result published_snapshots",
        kind="published",
        maximum=MAX_PUBLISHED_SNAPSHOTS,
    )
    if current not in published:
        raise SnapshotMaintenanceReconcileError(
            "saved apply result current must be present in published_snapshots"
        )
    expected_revision = _require_revision(
        result.get("expected_maintenance_revision"),
        "saved apply result expected_maintenance_revision",
    )
    observed_revision = _require_revision(
        result.get("observed_maintenance_revision"),
        "saved apply result observed_maintenance_revision",
    )
    if expected_revision != observed_revision:
        raise SnapshotMaintenanceReconcileError(
            "saved apply result expected and observed maintenance revisions "
            "must match"
        )
    _require_revision(
        result.get("observed_retention_plan_revision"),
        "saved apply result observed_retention_plan_revision",
    )
    _require_revision(
        result.get("observed_staging_cleanup_plan_revision"),
        "saved apply result observed_staging_cleanup_plan_revision",
    )
    planned_snapshots = _require_canonical_name_list(
        result.get("planned_deletion_snapshots"),
        "saved apply result planned_deletion_snapshots",
        kind="published",
        maximum=MAX_PUBLISHED_SNAPSHOTS,
    )
    planned_staging = _require_canonical_name_list(
        result.get("planned_deletion_staging_entries"),
        "saved apply result planned_deletion_staging_entries",
        kind="staging",
        maximum=MAX_STAGING_ENTRIES,
    )
    if current in planned_snapshots or not set(planned_snapshots).issubset(published):
        raise SnapshotMaintenanceReconcileError(
            "saved apply result planned published candidates must be non-current "
            "members of published_snapshots"
        )
    deleted_snapshots = _require_string_list(
        result.get("deleted_snapshots"), "saved apply result deleted_snapshots"
    )
    deleted_staging = _require_string_list(
        result.get("deleted_staging_entries"),
        "saved apply result deleted_staging_entries",
    )
    not_attempted_snapshots = _require_string_list(
        result.get("not_attempted_snapshots"),
        "saved apply result not_attempted_snapshots",
    )
    not_attempted_staging = _require_string_list(
        result.get("not_attempted_staging_entries"),
        "saved apply result not_attempted_staging_entries",
    )
    failed_snapshot = _require_optional_string(
        result.get("failed_snapshot"), "saved apply result failed_snapshot"
    )
    failed_staging = _require_optional_string(
        result.get("failed_staging_entry"),
        "saved apply result failed_staging_entry",
    )
    ok = result.get("ok")
    changed = result.get("changed")
    partial = result.get("partial")
    filesystem_changed = result.get("filesystem_may_have_changed")
    retry = result.get("retry_requires_fresh_plan")
    if any(
        not isinstance(flag, bool)
        for flag in (ok, changed, partial, filesystem_changed, retry)
    ):
        raise SnapshotMaintenanceReconcileError(
            "saved apply result ok/changed/partial flags must be booleans"
        )
    if ok is not (not partial):
        raise SnapshotMaintenanceReconcileError(
            "saved apply result ok must be the negation of partial"
        )
    if changed is not bool(deleted_snapshots or deleted_staging):
        raise SnapshotMaintenanceReconcileError(
            "saved apply result changed must reflect completed candidate deletions"
        )
    if filesystem_changed is not bool(changed or partial):
        raise SnapshotMaintenanceReconcileError(
            "saved apply result filesystem_may_have_changed is inconsistent"
        )
    if retry is not bool(partial):
        raise SnapshotMaintenanceReconcileError(
            "saved apply result retry_requires_fresh_plan must match partial"
        )
    if result.get("maintenance_confirmed") is not True:
        raise SnapshotMaintenanceReconcileError(
            "saved apply result maintenance_confirmed must be true"
        )
    actionable = _require_string_list(
        result.get("actionable_components"),
        "saved apply result actionable_components",
    )
    expected_actionable = _byte_sort(
        [
            component
            for component, candidates in (
                (_COMPONENT_PRUNE, planned_snapshots),
                (_COMPONENT_CLEANUP, planned_staging),
            )
            if candidates
        ]
    )
    if actionable != expected_actionable:
        raise SnapshotMaintenanceReconcileError(
            "saved apply result actionable_components does not match its "
            "planned deletion sets"
        )
    component_order = _require_string_list(
        result.get("component_apply_order"),
        "saved apply result component_apply_order",
    )
    if component_order != list(_APPLY_ORDER):
        raise SnapshotMaintenanceReconcileError(
            "saved apply result component_apply_order is not the schema-1 order"
        )
    completed = _require_string_list(
        result.get("completed_components"), "saved apply result completed_components"
    )
    not_attempted_components = _require_string_list(
        result.get("not_attempted_components"),
        "saved apply result not_attempted_components",
    )
    stopped = result.get("stopped_on_component")
    if stopped is not None and stopped not in _APPLY_ORDER:
        raise SnapshotMaintenanceReconcileError(
            "saved apply result stopped_on_component is not a known component"
        )
    execution = [name for name in _APPLY_ORDER if name in expected_actionable]
    if partial:
        if stopped not in execution:
            raise SnapshotMaintenanceReconcileError(
                "saved apply result partial=true must stop on an actionable component"
            )
        stopped_index = execution.index(stopped)
        expected_not_attempted = execution[stopped_index + 1 :]
        if (
            completed != execution[:stopped_index]
            or not_attempted_components != expected_not_attempted
        ):
            raise SnapshotMaintenanceReconcileError(
                "saved apply result component outcome is not an exact apply-order "
                "prefix/stopped/suffix partition"
            )
    else:
        if (
            stopped is not None
            or completed != execution
            or not_attempted_components
        ):
            raise SnapshotMaintenanceReconcileError(
                "saved apply result complete success must complete every actionable "
                "component in apply order"
            )
    statuses = {
        component: (
            "completed"
            if component in completed
            else "stopped"
            if component == stopped
            else "not_attempted"
        )
        for component in execution
    }
    if _COMPONENT_CLEANUP in statuses:
        _validate_component_outcome(
            planned_staging,
            deleted_staging,
            failed_staging,
            not_attempted_staging,
            component=_COMPONENT_CLEANUP,
            status=statuses[_COMPONENT_CLEANUP],
        )
    elif deleted_staging or failed_staging is not None or not_attempted_staging:
        raise SnapshotMaintenanceReconcileError(
            "saved apply result has a staging outcome without staging candidates"
        )
    if _COMPONENT_PRUNE in statuses:
        _validate_component_outcome(
            planned_snapshots,
            deleted_snapshots,
            failed_snapshot,
            not_attempted_snapshots,
            component=_COMPONENT_PRUNE,
            status=statuses[_COMPONENT_PRUNE],
        )
    elif deleted_snapshots or failed_snapshot is not None or not_attempted_snapshots:
        raise SnapshotMaintenanceReconcileError(
            "saved apply result has a published outcome without published candidates"
        )
    if stopped == _COMPONENT_CLEANUP and failed_staging is None:
        raise SnapshotMaintenanceReconcileError(
            "saved apply result stopped cleanup must identify its failed candidate"
        )
    if stopped == _COMPONENT_PRUNE and failed_snapshot is None and (
        deleted_snapshots
        or not_attempted_snapshots != planned_snapshots
        or _COMPONENT_CLEANUP not in completed
    ):
        raise SnapshotMaintenanceReconcileError(
            "saved apply result stopped prune without a failed candidate is only "
            "valid after completed staging cleanup and before prune deletion"
        )
    error_text = _require_optional_string(
        result.get("error"), "saved apply result error"
    )
    if (partial and error_text is None) or (not partial and error_text is not None):
        raise SnapshotMaintenanceReconcileError(
            "saved apply result error must be non-empty exactly when partial=true"
        )
    _require_canonical_name_list(
        result.get("remaining_published_snapshots"),
        "saved apply result remaining_published_snapshots",
        kind="published",
        maximum=MAX_PUBLISHED_SNAPSHOTS,
    )
    _require_canonical_name_list(
        result.get("remaining_staging_entries"),
        "saved apply result remaining_staging_entries",
        kind="staging",
        maximum=MAX_STAGING_ENTRIES,
    )
    return dict(result)


def _validate_apply_result_matches_plan(
    result: Mapping[str, Any], plan: Mapping[str, Any]
) -> None:
    mismatches: List[str] = []
    if result.get("expected_maintenance_revision") != plan.get("maintenance_revision"):
        mismatches.append("expected_maintenance_revision")
    if result.get("observed_maintenance_revision") != plan.get("maintenance_revision"):
        mismatches.append("observed_maintenance_revision")
    if result.get("observed_retention_plan_revision") != plan["retention_plan"].get(
        "plan_revision"
    ):
        mismatches.append("observed_retention_plan_revision")
    if result.get("observed_staging_cleanup_plan_revision") != plan[
        "staging_cleanup_plan"
    ].get("plan_revision"):
        mismatches.append("observed_staging_cleanup_plan_revision")
    if list(result.get("planned_deletion_snapshots") or []) != list(
        plan["retention_plan"].get("deletion_candidates") or []
    ):
        mismatches.append("planned_deletion_snapshots")
    if list(result.get("planned_deletion_staging_entries") or []) != list(
        plan["staging_cleanup_plan"].get("deletion_candidates") or []
    ):
        mismatches.append("planned_deletion_staging_entries")
    if result.get("graph") != plan.get("graph"):
        mismatches.append("graph")
    if result.get("keep_last") != plan.get("keep_last"):
        mismatches.append("keep_last")
    if result.get("current") != plan.get("current"):
        mismatches.append("current")
    if list(result.get("published_snapshots") or []) != list(
        plan.get("published_snapshots") or []
    ):
        mismatches.append("published_snapshots")
    if list(result.get("actionable_components") or []) != list(
        plan.get("actionable_components") or []
    ):
        mismatches.append("actionable_components")
    expected_remaining_published = [
        name
        for name in plan.get("published_snapshots") or []
        if name not in set(result.get("deleted_snapshots") or [])
    ]
    if list(result.get("remaining_published_snapshots") or []) != (
        expected_remaining_published
    ):
        mismatches.append("remaining_published_snapshots")
    blocked_names = [
        str(item.get("name"))
        for item in plan["staging_cleanup_plan"].get("blocked_entries") or []
    ]
    expected_remaining_staging = _byte_sort(
        list(
            dict.fromkeys(
                [
                    *blocked_names,
                    *(
                        [str(result.get("failed_staging_entry"))]
                        if result.get("failed_staging_entry") is not None
                        else []
                    ),
                    *(result.get("not_attempted_staging_entries") or []),
                ]
            )
        )
    )
    if list(result.get("remaining_staging_entries") or []) != (
        expected_remaining_staging
    ):
        mismatches.append("remaining_staging_entries")
    if mismatches:
        raise SnapshotMaintenanceReconcileIntegrityError(
            "saved apply result refers to another plan: " + ", ".join(mismatches)
        )


def _observe_candidate(path: Path) -> Dict[str, Any]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {
            "state": _STATE_ABSENT,
            "entry_kind": "absent",
            "identity": None,
        }
    except OSError as error:
        raise SnapshotMaintenanceReconcileIntegrityError(
            f"cannot inspect planned candidate {path}: {error}"
        ) from error
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_mtime_ns, info.st_size)
    if stat.S_ISLNK(info.st_mode):
        return {
            "state": _STATE_SYMLINK,
            "entry_kind": "symlink",
            "identity": identity,
        }
    if stat.S_ISDIR(info.st_mode):
        return {
            "state": _STATE_DIRECTORY,
            "entry_kind": "directory",
            "identity": identity,
        }
    return {
        "state": _STATE_NON_DIRECTORY,
        "entry_kind": _entry_kind(info.st_mode),
        "identity": identity,
    }


def _observe_writer_lock(path: Path, state: str) -> Dict[str, Any]:
    empty = {
        "writer_lease_protocol": None,
        "writer_lease_state": None,
        "writer_lock_present": None,
        "writer_lock_regular": None,
        "ownership_status": "unknown",
    }
    if state != _STATE_DIRECTORY:
        return empty
    try:
        observation = probe_staging_writer_lease(path)
    except StagingWriterLockUnsafe:
        return {
            "writer_lease_protocol": "unsafe",
            "writer_lease_state": "unverifiable",
            "writer_lock_present": None,
            "writer_lock_regular": False,
            "ownership_status": "unknown",
        }
    except StagingWriterLeaseError:
        return {
            "writer_lease_protocol": None,
            "writer_lease_state": "unverifiable",
            "writer_lock_present": None,
            "writer_lock_regular": None,
            "ownership_status": "unknown",
        }
    return {
        "writer_lease_protocol": observation.get("writer_lease_protocol"),
        "writer_lease_state": observation.get("writer_lease_state"),
        "writer_lock_present": observation.get("writer_lock_present"),
        "writer_lock_regular": observation.get("writer_lock_regular"),
        "ownership_status": "unknown",
    }


def _list_published_names(root: Path, planned_names: Sequence[str]) -> List[str]:
    snapshots = root / "snapshots"
    planned = set(planned_names)
    try:
        _info, entries = _safe_directory_entries(
            snapshots,
            max_entries=MAX_PUBLISHED_SNAPSHOTS + MAX_STAGING_ENTRIES,
            label="snapshots directory",
        )
    except SnapshotStagingError as error:
        raise _wrap_staging_error(error) from error
    published: List[str] = []
    for name, entry in entries:
        path = snapshots / name
        planned_here = name in planned
        if stat.S_ISLNK(entry.st_mode):
            if planned_here:
                continue
            raise SnapshotMaintenanceReconcileIntegrityError(
                f"unsafe symlinked snapshot entry: {path}"
            )
        if is_staging_snapshot_name(name):
            continue
        if is_published_snapshot_id(name) and stat.S_ISDIR(entry.st_mode):
            published.append(name)
            if len(published) > MAX_PUBLISHED_SNAPSHOTS:
                raise SnapshotMaintenanceReconcileError(
                    "published snapshot count exceeds bound "
                    f"{MAX_PUBLISHED_SNAPSHOTS}"
                )
            continue
        if planned_here:
            continue
        raise SnapshotMaintenanceReconcileError(
            f"unexpected unsafe snapshots entry is not published history: {path}"
        )
    return _byte_sort(published)


def _scan_graph(root: Path, plan: Mapping[str, Any]) -> Dict[str, Any]:
    _require_managed_graph(root)
    try:
        lock_identity = _lock_identity(root)
        current_id, current_identity = _read_current(root)
    except SnapshotStagingError as error:
        raise _wrap_staging_error(error) from error
    planned_snapshots = list(plan["retention_plan"]["deletion_candidates"])
    planned_staging = list(plan["staging_cleanup_plan"]["deletion_candidates"])
    published = _list_published_names(
        root, [*planned_snapshots, *planned_staging]
    )
    if current_id not in published:
        raise SnapshotMaintenanceReconcileIntegrityError(
            f"current snapshot is missing or dangling: {current_id!r}"
        )
    snapshots = root / "snapshots"
    published_observations = {
        name: _observe_candidate(snapshots / name) for name in planned_snapshots
    }
    staging_observations = {
        name: _observe_candidate(snapshots / name) for name in planned_staging
    }
    return {
        "lock_identity": lock_identity,
        "current": current_id,
        "current_identity": current_identity,
        "published": published,
        "stable_published": [
            name for name in published if name not in set(planned_snapshots)
        ],
        "published_observations": published_observations,
        "staging_observations": staging_observations,
    }


def _same_observation(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    return first.get("state") == second.get("state") and first.get(
        "identity"
    ) == second.get("identity")


def _declared_status(
    name: str,
    *,
    deleted: Sequence[str],
    failed: Optional[str],
    not_attempted: Sequence[str],
) -> Optional[str]:
    if name in deleted:
        return _DECLARED_DELETED
    if name == failed:
        return _DECLARED_FAILED
    if name in not_attempted:
        return _DECLARED_NOT_ATTEMPTED
    return None


def _public_observation(
    name: str,
    observed: Mapping[str, Any],
    *,
    declared: Optional[str],
    writer: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": name,
        "state": observed["state"],
        "entry_kind": observed["entry_kind"],
        "declared_status": declared,
    }
    if writer is not None:
        payload.update(writer)
    return payload


def _discrepancy(
    code: str,
    message: str,
    *,
    component: str,
    name: str,
    declared: Optional[str],
    observed_state: str,
) -> Dict[str, str]:
    payload = {
        "code": code,
        "kind": "discrepancy",
        "component": component,
        "name": name,
        "message": message,
        "observed_state": observed_state,
    }
    if declared is not None:
        payload["declared_status"] = declared
    return payload


def _compare_declaration(
    name: str,
    observed: Mapping[str, Any],
    declared: Optional[str],
    *,
    component: str,
) -> Optional[Dict[str, str]]:
    state = str(observed.get("state"))
    if declared == _DECLARED_DELETED and state != _STATE_ABSENT:
        return _discrepancy(
            "declared_deleted_but_present",
            "apply result declared this candidate completely deleted, but "
            "the pathname is not absent at reconcile",
            component=component,
            name=name,
            declared=declared,
            observed_state=state,
        )
    if declared in {_DECLARED_FAILED, _DECLARED_NOT_ATTEMPTED} and state != _STATE_DIRECTORY:
        return _discrepancy(
            "declared_remaining_but_not_directory",
            "apply result declared this candidate failed or not attempted, "
            "but it is not a present directory at reconcile",
            component=component,
            name=name,
            declared=declared,
            observed_state=state,
        )
    return None


def _reconcile_unlocked(
    root: Path,
    plan: Mapping[str, Any],
    apply_result: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    first = _scan_graph(root, plan)
    _after_first_reconcile_scan(root, first)
    second = _scan_graph(root, plan)
    if (
        first["lock_identity"] != second["lock_identity"]
        or first["current"] != second["current"]
        or first["current_identity"] != second["current_identity"]
        or first["stable_published"] != second["stable_published"]
    ):
        raise SnapshotMaintenanceReconcileIntegrityError(
            "graph current, published listing, or publication lock changed "
            "during reconcile"
        )
    planned_snapshots = list(plan["retention_plan"]["deletion_candidates"])
    planned_staging = list(plan["staging_cleanup_plan"]["deletion_candidates"])
    snapshots = root / "snapshots"
    published_observations: List[Dict[str, Any]] = []
    staging_observations: List[Dict[str, Any]] = []
    deleted_snapshots = list((apply_result or {}).get("deleted_snapshots") or [])
    deleted_staging = list((apply_result or {}).get("deleted_staging_entries") or [])
    failed_snapshot = (apply_result or {}).get("failed_snapshot")
    failed_staging = (apply_result or {}).get("failed_staging_entry")
    not_attempted_snapshots = list(
        (apply_result or {}).get("not_attempted_snapshots") or []
    )
    not_attempted_staging = list(
        (apply_result or {}).get("not_attempted_staging_entries") or []
    )
    discrepancies: List[Dict[str, str]] = []
    for name in planned_snapshots:
        first_obs = first["published_observations"][name]
        second_obs = second["published_observations"][name]
        observed = dict(second_obs)
        if not _same_observation(first_obs, second_obs):
            observed["state"] = _STATE_CHANGED
        declared = None
        if apply_result is not None:
            declared = _declared_status(
                name,
                deleted=deleted_snapshots,
                failed=failed_snapshot,
                not_attempted=not_attempted_snapshots,
            )
        published_observations.append(
            _public_observation(name, observed, declared=declared)
        )
        if apply_result is not None:
            found = _compare_declaration(
                name, observed, declared, component=_COMPONENT_PRUNE
            )
            if found is not None:
                discrepancies.append(found)
    for name in planned_staging:
        first_obs = first["staging_observations"][name]
        second_obs = second["staging_observations"][name]
        observed = dict(second_obs)
        if not _same_observation(first_obs, second_obs):
            observed["state"] = _STATE_CHANGED
        declared = None
        if apply_result is not None:
            declared = _declared_status(
                name,
                deleted=deleted_staging,
                failed=failed_staging,
                not_attempted=not_attempted_staging,
            )
        writer = _observe_writer_lock(snapshots / name, str(observed["state"]))
        staging_observations.append(
            _public_observation(name, observed, declared=declared, writer=writer)
        )
        if apply_result is not None:
            found = _compare_declaration(
                name, observed, declared, component=_COMPONENT_CLEANUP
            )
            if found is not None:
                discrepancies.append(found)
    if second["current"] != plan["current"]:
        discrepancies.append(
            {
                "code": "current_differs_from_saved_plan",
                "kind": "discrepancy",
                "component": "graph",
                "name": "current",
                "message": "observed current does not match the saved plan",
                "observed_state": str(second["current"]),
            }
        )
    all_absent = all(
        item["state"] == _STATE_ABSENT
        for item in (*published_observations, *staging_observations)
    )
    consistent: Optional[bool]
    if apply_result is None:
        consistent = None
    else:
        consistent = not any(
            item.get("code")
            in {
                "declared_deleted_but_present",
                "declared_remaining_but_not_directory",
                "current_differs_from_saved_plan",
            }
            for item in discrepancies
        )
    return {
        "schema_version": RECONCILE_SCHEMA_VERSION,
        "ok": True,
        "graph": str(root),
        "keep_last": plan["keep_last"],
        "input_plan_revision": plan["maintenance_revision"],
        "input_plan_valid": True,
        "apply_result_supplied": apply_result is not None,
        "apply_result_valid": True if apply_result is not None else None,
        "observed_current": second["current"],
        "observed_published_snapshots": list(second["published"]),
        "current_matches_saved_plan": second["current"] == plan["current"],
        "reconciliation_is_observation_only": True,
        "deletion_cause_proven": False,
        "recovery_performed": False,
        "planned_deletion_snapshots": planned_snapshots,
        "planned_deletion_staging_entries": planned_staging,
        "published_candidate_observations": published_observations,
        "staging_candidate_observations": staging_observations,
        "all_planned_candidates_absent_at_reconcile": all_absent,
        "result_consistent_with_observation": consistent,
        "input_byte_limit": MAX_INPUT_BYTES,
        "discrepancies": discrepancies,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }


@contextmanager
def _snapshot_maintenance_reconcile_scope(
    graph: Path,
    plan_file: Path,
    apply_result_file: Optional[Path],
) -> Iterator[Dict[str, Any]]:
    """Yield one reconciliation while its shared existing-lock lease remains held."""
    plan = _validate_saved_plan(_load_json_object(plan_file, label="plan-file"))
    apply_result: Optional[Dict[str, Any]] = None
    if apply_result_file is not None:
        loaded = _validate_apply_result_structure(
            _load_json_object(apply_result_file, label="apply-result-file")
        )
        _validate_apply_result_matches_plan(loaded, plan)
        apply_result = loaded
    root = _resolve_graph_root(graph)
    _require_saved_plan_graph(plan, root)
    _require_managed_graph(root)
    try:
        with graph_read_lease(root, allow_unlocked_managed=False):
            yield _reconcile_unlocked(root, plan, apply_result)
    except SnapshotMaintenanceReconcileError:
        raise
    except SnapshotStagingError as error:
        raise _wrap_staging_error(error) from error
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_maintenance_reconcile(
    graph: Path,
    plan_file: Path,
    apply_result_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reconcile a saved maintenance plan against the live graph.

    Returns the schema-1 observation. ``ok=true`` means this read
    completed. Remaining candidates or discrepancies do not change the
    exit code. Pre-response failures raise
    :class:`SnapshotMaintenanceReconcileError` (exit 2) or
    :class:`SnapshotMaintenanceReconcileIntegrityError` (exit 1).
    """
    with _snapshot_maintenance_reconcile_scope(
        graph, plan_file, apply_result_file
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Observe the aftermath of a saved snapshot-maintenance-plan. "
            "Optional --apply-result-file compares a saved "
            "snapshot-maintenance-apply result. Observation-only: no "
            "recovery, rollback, or deletion-cause claim. Never creates "
            ".publish.lock, and is not an MCP tool. Input files are "
            f"regular files bounded at {MAX_INPUT_BYTES} bytes."
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
        "--plan-file",
        type=Path,
        required=True,
        help="Saved schema-1 snapshot-maintenance-plan JSON, relative to cwd.",
    )
    parser.add_argument(
        "--apply-result-file",
        type=Path,
        default=None,
        help=(
            "Optional saved schema-1 snapshot-maintenance-apply JSON, "
            "relative to cwd."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_maintenance_reconcile_scope(
            args.graph,
            args.plan_file,
            args.apply_result_file,
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing the graph lease so
            # the complete response is handed to the caller under that lease.
            sys.stdout.flush()
    except SnapshotMaintenanceReconcileError as error:
        print(f"snapshot-maintenance-reconcile: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-maintenance-reconcile: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
