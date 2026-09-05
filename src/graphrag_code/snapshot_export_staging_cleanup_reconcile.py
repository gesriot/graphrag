#!/usr/bin/env python
"""Read-only snapshot-export staging cleanup reconciliation.

``snapshot-export-staging-cleanup-reconcile`` inspects the aftermath of
a saved schema-2 ``snapshot-export-staging-cleanup-plan`` and an
optional saved schema-1 ``snapshot-export-staging-cleanup`` result. It
is observation-only. It does not delete, rename, quarantine, recover,
retry, repair, chmod, truncate, create, replace, or otherwise mutate
any filesystem entry. It does not claim a writer lease, inspect a
managed graph, or expose an apply token.

Absence does not prove cleanup apply deleted an entry. Presence does
not prove cleanup failed. Matching identity does not prove ownership
or continuous identity. Held/non-held writer-lease observation does
not prove liveness or death. Advisory locks protect only cooperating
processes. Reconciliation performs no recovery or mutation. It is not
a retry token or authorization to delete. A fresh standalone schema-2
cleanup plan is required before any later apply. This report is not
backup, authenticity, provenance, or recoverability evidence.

Both input paths are relative to the invoking cwd unless absolute.
Only bounded regular files are accepted; they are opened read-only
without following symlinks. The conservative limit is
``MAX_INPUT_BYTES`` (1 MiB). Complete plan and apply-result validation
finish before the parent is observed. Malformed, oversized, symlinked,
replaced, truncated, or structurally invalid inputs fail with exit 2
and empty stdout. A structurally valid apply result that refers to
another plan or parent is an integrity failure: exit 1 and empty
stdout.

Parent observation reuses the snapshot-export-staging
descriptor-relative, no-follow, bounded two-scan contract internally.
It does not invoke a public CLI. The parent descriptor plus retained
recognized staging and lock descriptors stay open through result
construction, serialization, stdout write, and flush. MCP stays
exactly 17 read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-export-staging-cleanup-reconcile \\
        --parent <directory> --plan-file <saved-cleanup-plan.json> \\
        [--apply-result-file <saved-cleanup-result.json>] [--json]
    python -m graphrag_code.snapshot_export_staging_cleanup_reconcile \\
        --parent <directory> --plan-file <saved-cleanup-plan.json> \\
        [--apply-result-file <saved-cleanup-result.json>] [--json]
    uv run python scripts/snapshot_export_staging_cleanup_reconcile.py \\
        --parent <directory> --plan-file <saved-cleanup-plan.json> \\
        [--apply-result-file <saved-cleanup-result.json>] [--json]
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
    MAX_PREFIXED_ENTRIES,
    STAGING_SCHEMA_VERSION,
    SnapshotExportStagingError,
    SnapshotExportStagingIntegrityError,
    export_staging_observation_scope,
    inventory_revision_of,
    is_current_export_staging_name,
    is_export_staging_prefix_name,
)
from graphrag_code.snapshot_export_staging_cleanup_plan import (
    PLAN_SCHEMA_VERSION,
    SnapshotExportStagingCleanupPlanError,
    _classify_entry,
    _plan_from_inventory,
    plan_revision_of,
)
from graphrag_code.snapshot_export_writer_lease import (
    EXPORT_STAGING_WRITER_LOCK_NAME,
    WRITER_LEASE_HELD_AT_SCAN,
    WRITER_LEASE_METADATA_ABSENT,
    WRITER_LEASE_METADATA_UNSAFE,
    WRITER_LEASE_NOT_HELD_AT_SCAN,
)

RECONCILE_SCHEMA_VERSION = 1
APPLY_SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1 * 1024 * 1024
_MAX_ERROR_CHARS = 400
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_STATE_ABSENT = "absent_at_reconcile"
_STATE_CANDIDATE = "present_candidate_at_reconcile"
_STATE_BLOCKED = "present_blocked_at_reconcile"
_STATE_NON_DIRECTORY = "present_non_directory_at_reconcile"
_STATE_SYMLINK = "unsafe_symlink_at_reconcile"
_DECLARED_NOT_SUPPLIED = "not_supplied"
_DECLARED_COMPLETE = "complete"
_DECLARED_PARTIAL = "partial"
_IDENTITY_KEYS = ("dev", "ino", "size", "mtime_ns", "ctime_ns", "mode")
_ENTRY_KINDS = frozenset(
    {"symlink", "directory", "file", "fifo", "socket", "device", "other"}
)
_ENTRY_COMMON_KEYS = frozenset(
    {
        "name",
        "path",
        "kind",
        "name_matches_current_protocol",
        "ownership",
        "writer_activity",
        "cleanup_eligible",
        "contents_inspected",
        *_IDENTITY_KEYS,
    }
)
_WRITER_IDENTITY_KEYS = (
    "writer_lease_dev",
    "writer_lease_ino",
    "writer_lease_mode",
    "writer_lease_size",
    "writer_lease_mtime_ns",
    "writer_lease_ctime_ns",
)
_WRITER_KEYS = frozenset(
    {
        "writer_lease_state",
        "writer_lease_metadata_present",
        "writer_lease_contended",
        "writer_lease_path",
        *_WRITER_IDENTITY_KEYS,
    }
)
_WRITER_STATES = frozenset(
    {
        WRITER_LEASE_METADATA_ABSENT,
        WRITER_LEASE_METADATA_UNSAFE,
        WRITER_LEASE_HELD_AT_SCAN,
        WRITER_LEASE_NOT_HELD_AT_SCAN,
    }
)
_PLAN_KEYS = frozenset(
    {
        "schema_version",
        "ok",
        "parent",
        "observed_inventory_revision",
        "staging_entries",
        "staging_count",
        "unrecognized_prefixed_entries",
        "unrecognized_prefixed_count",
        "other_entry_count",
        "deletion_candidates",
        "deletion_candidate_count",
        "blocked_entries",
        "blocked_count",
        "ownership_inference",
        "cleanup_applied",
        "apply_supported",
        "plan_revision",
        "notices",
    }
)
_APPLY_KEYS = frozenset(
    {
        "schema_version",
        "ok",
        "parent",
        "expected_plan_revision",
        "observed_plan_revision",
        "planned_deletion_candidates",
        "deleted_staging_entries",
        "deleted_count",
        "remaining_staging_entries",
        "changed",
        "cleanup_confirmed",
        "partial",
        "filesystem_may_have_changed",
        "retry_requires_fresh_plan",
        "failed_staging_entry",
        "not_attempted_staging_entries",
        "ownership_inference",
        "error",
        "notices",
    }
)
_BLOCKED_REASONS = frozenset(
    {
        "unrecognized_staging_name",
        "non_directory_staging_entry",
        "writer_lease_metadata_absent",
        "writer_lease_metadata_unsafe",
        "held_writer_lease",
        "unverifiable_writer_lease_state",
        "nonempty_writer_lease_metadata",
        "permissive_writer_lease_metadata",
    }
)
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "reconciliation_is_observation_only",
        "kind": "notice",
        "message": (
            "snapshot-export-staging-cleanup-reconcile is observation-only. "
            "ok means this read completed, not that cleanup apply succeeded, "
            "that every candidate is absent, or that a saved apply result "
            "agrees with the current filesystem."
        ),
    },
    {
        "code": "absence_does_not_prove_cleanup_deleted",
        "kind": "notice",
        "message": (
            "An absent pathname does not prove snapshot-export-staging-cleanup "
            "deleted the entry. deletion_cause_proven is always false. "
            "Another actor may have removed or replaced the name."
        ),
    },
    {
        "code": "presence_does_not_prove_cleanup_failed",
        "kind": "notice",
        "message": (
            "A present pathname does not prove snapshot-export-staging-cleanup "
            "failed. Another actor may have created or restored the name "
            "after apply."
        ),
    },
    {
        "code": "identity_equality_is_observation_window_only",
        "kind": "notice",
        "message": (
            "identity_matches_saved_observation is only equality of observed "
            "metadata during the saved-plan and reconcile windows. It is not "
            "ownership, provenance, or proof of continuous identity."
        ),
    },
    {
        "code": "writer_lease_is_not_liveness",
        "kind": "notice",
        "message": (
            "A held or non-held writer-lease observation does not prove "
            "liveness or death. Advisory locks protect only cooperating "
            "processes."
        ),
    },
    {
        "code": "no_recovery_performed",
        "kind": "notice",
        "message": (
            "recovery_performed is always false. This command does not "
            "recover, retry, repair, rename, quarantine, delete, chmod, "
            "truncate, create, or mutate anything, and it is not "
            "authorization to delete anything."
        ),
    },
    {
        "code": "fresh_plan_required_before_mutation",
        "kind": "notice",
        "message": (
            "A fresh snapshot-export-staging-cleanup-plan is still required "
            "before any later apply. This reconciliation is not a retry "
            "token and does not emit a current cleanup plan_revision."
        ),
    },
    {
        "code": "not_backup_or_authenticity",
        "kind": "notice",
        "message": (
            "This report is not backup, authenticity, provenance, or "
            "recoverability evidence."
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
            "snapshot-export-staging-cleanup-reconcile is CLI-only and "
            "intentionally absent from the fixed 14-tool MCP set."
        ),
    },
)


class SnapshotExportStagingCleanupReconcileError(Exception):
    """Malformed arguments, inputs, or unsupported invocation. Default exit 2."""

    exit_code = 2


class SnapshotExportStagingCleanupReconcileIntegrityError(
    SnapshotExportStagingCleanupReconcileError
):
    """Parent mismatch, unsafe structure, concurrent change, or result mismatch. Exit 1."""

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
        "snapshot-export-staging-cleanup-reconcile: "
        f"parent={result.get('parent')} "
        f"input_plan_revision={result.get('input_plan_revision')} "
        "apply_result_supplied="
        f"{str(result.get('apply_result_supplied')).lower()} "
        f"declared_apply_outcome={result.get('declared_apply_outcome')} "
        "all_planned_candidates_absent_at_reconcile="
        f"{str(result.get('all_planned_candidates_absent_at_reconcile')).lower()} "
        f"result_consistent_with_observation={consistent_text} "
        f"discrepancies={len(result.get('discrepancies') or [])} "
        "reconciliation_is_observation_only=true "
        "deletion_cause_proven=false "
        "fresh_plan_required_before_mutation=true"
    )


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(values, key=os.fsencode)


def _byte_sort_unique(values: Sequence[str]) -> List[str]:
    return sorted(dict.fromkeys(values), key=os.fsencode)


def _file_identity(info: os.stat_result) -> Tuple[int, int, int, int, int, int]:
    """Complete regular-file token, including ctime."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _after_input_path_lstat(_path: Path) -> None:
    """Test hook after lstat and before the no-follow input open."""
    return None


def _after_input_opened(_path: Path, _fd: int) -> None:
    """Test hook after the input descriptor is accepted and before the read."""
    return None


def _after_result_ready(_parent: Path, _result: Mapping[str, Any]) -> None:
    """Test hook after the result is built and before it is yielded."""
    return None


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant {value!r}")


def _is_canonical_direct_name(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and Path(name).name == name
    )


def _require_bool(
    value: object, label: str, *, expected: Optional[bool] = None
) -> bool:
    if not isinstance(value, bool):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be a boolean"
        )
    if expected is not None and value is not expected:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be {str(expected).lower()}"
        )
    return value


def _require_non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be a non-empty string"
        )
    return value


def _require_absolute_string(value: object, label: str) -> str:
    text = _require_non_empty_string(value, label)
    if not Path(text).is_absolute():
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be an absolute path"
        )
    return text


def _require_revision(value: object, label: str) -> str:
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be sha256:<64 lowercase hex>, got {value!r}"
        )
    return value


def _require_int(value: object, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be an integer"
        )
    if value < minimum:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be >= {minimum}"
        )
    return value


def _require_string_list(value: object, label: str) -> List[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) for item in value
    ):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be an array of strings"
        )
    return list(value)


def _require_optional_string(value: object, label: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or value == "":
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be a non-empty string or null"
        )
    return value


def _require_notices(value: object, label: str) -> List[Any]:
    if not isinstance(value, list):
        raise SnapshotExportStagingCleanupReconcileError(f"{label} must be an array")
    return list(value)


def _wrap_staging_error(error: Exception) -> SnapshotExportStagingCleanupReconcileError:
    if isinstance(error, SnapshotExportStagingIntegrityError):
        return SnapshotExportStagingCleanupReconcileIntegrityError(str(error))
    if isinstance(error, SnapshotExportStagingError):
        wrapped = SnapshotExportStagingCleanupReconcileError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    return SnapshotExportStagingCleanupReconcileError(str(error))


def _read_bounded_regular_file(path: Path, *, label: str) -> bytes:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    try:
        before = resolved.lstat()
    except FileNotFoundError as error:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} does not exist: {resolved}"
        ) from error
    except OSError as error:
        raise SnapshotExportStagingCleanupReconcileError(
            f"cannot inspect {label} {resolved}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be a regular file, not a symlink: {resolved}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} is not a regular file: {resolved}"
        )
    if before.st_size > MAX_INPUT_BYTES:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} exceeds bound {MAX_INPUT_BYTES} bytes: {resolved}"
        )
    if not hasattr(os, "O_NOFOLLOW"):
        raise SnapshotExportStagingCleanupReconcileError(
            "safe no-follow input-file reads are unsupported on "
            f"this platform: {sys.platform!r}"
        )
    expected = _file_identity(before)
    _after_input_path_lstat(resolved)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(resolved), flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT}:
            raise SnapshotExportStagingCleanupReconcileError(
                f"{label} changed or became unsafe while opening it: {resolved}"
            ) from error
        raise SnapshotExportStagingCleanupReconcileError(
            f"cannot safely open {label} {resolved}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = resolved.lstat()
        except OSError as error:
            raise SnapshotExportStagingCleanupReconcileError(
                f"{label} changed while opening it: {resolved}"
            ) from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _file_identity(opened) != expected
            or _file_identity(current) != expected
        ):
            raise SnapshotExportStagingCleanupReconcileError(
                f"{label} changed or became unsafe while opening it: {resolved}"
            )
        _after_input_opened(resolved, fd)
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
            raise SnapshotExportStagingCleanupReconcileError(
                f"{label} exceeds bound {MAX_INPUT_BYTES} bytes: {resolved}"
            )
        after_fd = os.fstat(fd)
        try:
            after_path = resolved.lstat()
        except OSError as error:
            raise SnapshotExportStagingCleanupReconcileError(
                f"{label} changed while it was read: {resolved}"
            ) from error
        if (
            _file_identity(after_fd) != expected
            or stat.S_ISLNK(after_path.st_mode)
            or not stat.S_ISREG(after_path.st_mode)
            or _file_identity(after_path) != expected
            or len(data) != expected[2]
        ):
            raise SnapshotExportStagingCleanupReconcileError(
                f"{label} changed while it was read: {resolved}"
            )
    finally:
        os.close(fd)
    return data


def _load_json_object(path: Path, *, label: str) -> Dict[str, Any]:
    data = _read_bounded_regular_file(path, label=label)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} is not valid UTF-8: {path}"
        ) from error
    try:
        parsed = json.loads(text, parse_constant=_reject_nonstandard_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} is not valid JSON: {error}"
        ) from error
    if not isinstance(parsed, dict):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be a JSON object"
        )
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
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} exceeds entry bound {maximum}"
        )
    try:
        for name in names:
            name.encode("utf-8")
    except UnicodeEncodeError as error:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} contains a string that is not valid UTF-8"
        ) from error
    if any(not _is_canonical_direct_name(name) for name in names):
        invalid = next(
            name for name in names if not _is_canonical_direct_name(name)
        )
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} contains a non-canonical direct name: {invalid!r}"
        )
    if len(set(names)) != len(names) or names != _byte_sort(names):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be unique and sorted in raw filesystem-byte order"
        )
    if kind == "current":
        invalid_kind = [
            name for name in names if not is_current_export_staging_name(name)
        ]
    elif kind == "prefixed":
        invalid_kind = [
            name for name in names if not is_export_staging_prefix_name(name)
        ]
    else:  # pragma: no cover - internal programming error
        raise AssertionError(f"unknown name kind: {kind}")
    if invalid_kind:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} contains a non-canonical {kind} name: {invalid_kind[0]!r}"
        )
    return names


def _entry_identity(entry: Mapping[str, Any]) -> Optional[Tuple[Any, ...]]:
    try:
        return tuple(entry[key] for key in _IDENTITY_KEYS)
    except KeyError:
        return None


def _require_identity_int(
    value: object, label: str, *, nonnegative: bool
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be an integer"
        )
    if nonnegative and value < 0:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be >= 0"
        )
    return value


def _kind_from_mode(mode: int) -> str:
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


def _validate_saved_inventory_entry(
    entry: object,
    *,
    parent: str,
    label: str,
    recognized: bool,
) -> Dict[str, Any]:
    if not isinstance(entry, dict):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} must be an object"
        )
    name = entry.get("name")
    if not isinstance(name, str) or not _is_canonical_direct_name(name):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label}.name must be a canonical direct name"
        )
    if recognized:
        if not is_current_export_staging_name(name):
            raise SnapshotExportStagingCleanupReconcileError(
                f"{label}.name is not a current-protocol export staging name"
            )
    elif is_current_export_staging_name(name) or not is_export_staging_prefix_name(
        name
    ):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label}.name is not an unrecognized export-staging prefix name"
        )

    kind = entry.get("kind")
    if not isinstance(kind, str) or kind not in _ENTRY_KINDS:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label}.kind is not a producer entry kind"
        )
    expected_keys = _ENTRY_COMMON_KEYS
    if recognized and kind == "directory":
        expected_keys = _ENTRY_COMMON_KEYS | _WRITER_KEYS
    if set(entry.keys()) != expected_keys:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} does not have the exact producer inventory fields"
        )

    expected_path = str(Path(parent) / name)
    if entry.get("path") != expected_path:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label}.path does not match its saved parent and name"
        )
    _require_bool(
        entry.get("name_matches_current_protocol"),
        f"{label}.name_matches_current_protocol",
        expected=recognized,
    )
    if entry.get("ownership") != "unknown":
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label}.ownership must be unknown"
        )
    if entry.get("writer_activity") != "unknown":
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label}.writer_activity must be unknown"
        )
    _require_bool(
        entry.get("cleanup_eligible"),
        f"{label}.cleanup_eligible",
        expected=False,
    )
    _require_bool(
        entry.get("contents_inspected"),
        f"{label}.contents_inspected",
        expected=False,
    )
    for key in ("dev", "ino", "size", "mode"):
        _require_identity_int(entry.get(key), f"{label}.{key}", nonnegative=True)
    for key in ("mtime_ns", "ctime_ns"):
        _require_identity_int(entry.get(key), f"{label}.{key}", nonnegative=False)
    mode = int(entry["mode"])
    if _kind_from_mode(mode) != kind:
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label}.kind does not match its mode"
        )

    if recognized and kind == "directory":
        state = entry.get("writer_lease_state")
        if not isinstance(state, str) or state not in _WRITER_STATES:
            raise SnapshotExportStagingCleanupReconcileError(
                f"{label}.writer_lease_state is not a producer state"
            )
        _require_bool(
            entry.get("writer_lease_metadata_present"),
            f"{label}.writer_lease_metadata_present",
            expected=state != WRITER_LEASE_METADATA_ABSENT,
        )
        _require_bool(
            entry.get("writer_lease_contended"),
            f"{label}.writer_lease_contended",
            expected=state == WRITER_LEASE_HELD_AT_SCAN,
        )
        expected_lock_path = str(
            Path(expected_path) / EXPORT_STAGING_WRITER_LOCK_NAME
        )
        if entry.get("writer_lease_path") != expected_lock_path:
            raise SnapshotExportStagingCleanupReconcileError(
                f"{label}.writer_lease_path does not match the protocol path"
            )
        if state == WRITER_LEASE_METADATA_ABSENT:
            if any(entry.get(key) is not None for key in _WRITER_IDENTITY_KEYS):
                raise SnapshotExportStagingCleanupReconcileError(
                    f"{label} has writer-lease identity for absent metadata"
                )
        else:
            for key in (
                "writer_lease_dev",
                "writer_lease_ino",
                "writer_lease_mode",
                "writer_lease_size",
            ):
                _require_identity_int(
                    entry.get(key), f"{label}.{key}", nonnegative=True
                )
            for key in ("writer_lease_mtime_ns", "writer_lease_ctime_ns"):
                _require_identity_int(
                    entry.get(key), f"{label}.{key}", nonnegative=False
                )
            if state in {
                WRITER_LEASE_HELD_AT_SCAN,
                WRITER_LEASE_NOT_HELD_AT_SCAN,
            } and not stat.S_ISREG(int(entry["writer_lease_mode"])):
                raise SnapshotExportStagingCleanupReconcileError(
                    f"{label}.writer_lease_mode must describe a regular file"
                )
    return dict(entry)


def _prefixed_entries(plan: Mapping[str, Any]) -> List[Dict[str, Any]]:
    staging = list(plan.get("staging_entries") or [])
    unrecognized = list(plan.get("unrecognized_prefixed_entries") or [])
    return staging + unrecognized


def _entry_names(entries: Sequence[Mapping[str, Any]], label: str) -> List[str]:
    names: List[str] = []
    for item in entries:
        if not isinstance(item, dict):
            raise SnapshotExportStagingCleanupReconcileError(
                f"{label} must contain objects"
            )
        name = item.get("name")
        if not isinstance(name, str) or not _is_canonical_direct_name(name):
            raise SnapshotExportStagingCleanupReconcileError(
                f"{label} contains a non-canonical direct name: {name!r}"
            )
        names.append(name)
    if len(set(names)) != len(names) or names != _byte_sort(names):
        raise SnapshotExportStagingCleanupReconcileError(
            f"{label} names must be unique and sorted in raw filesystem-byte order"
        )
    return names


def _recompute_inventory_revision(plan: Mapping[str, Any]) -> str:
    staging = list(plan.get("staging_entries") or [])
    unrecognized = list(plan.get("unrecognized_prefixed_entries") or [])
    unsafe = sum(1 for entry in staging if entry.get("kind") != "directory")
    payload = {
        "schema_version": STAGING_SCHEMA_VERSION,
        "staging_entries": staging,
        "staging_count": plan.get("staging_count"),
        "unsafe_staging_count": unsafe,
        "unrecognized_prefixed_entries": unrecognized,
        "unrecognized_prefixed_count": plan.get("unrecognized_prefixed_count"),
        "other_entry_count": plan.get("other_entry_count"),
    }
    try:
        return inventory_revision_of(payload)
    except (
        SnapshotExportStagingError,
        TypeError,
        ValueError,
        KeyError,
    ) as error:
        raise SnapshotExportStagingCleanupReconcileError(
            f"saved plan observed_inventory_revision cannot be recomputed: {error}"
        ) from error


def _validate_saved_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    schema = plan.get("schema_version")
    if isinstance(schema, bool) or schema != PLAN_SCHEMA_VERSION:
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan schema_version must be 2"
        )
    if set(plan.keys()) != _PLAN_KEYS:
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan does not have the exact schema-2 producer fields"
        )
    _require_bool(plan.get("ok"), "saved plan ok", expected=True)
    parent = _require_absolute_string(plan.get("parent"), "saved plan parent")
    staging = plan.get("staging_entries")
    unrecognized = plan.get("unrecognized_prefixed_entries")
    if not isinstance(staging, list) or not isinstance(unrecognized, list):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan staging_entries and unrecognized_prefixed_entries "
            "must be arrays"
        )
    for index, entry in enumerate(staging):
        _validate_saved_inventory_entry(
            entry,
            parent=parent,
            label=f"saved plan staging_entries[{index}]",
            recognized=True,
        )
    for index, entry in enumerate(unrecognized):
        _validate_saved_inventory_entry(
            entry,
            parent=parent,
            label=f"saved plan unrecognized_prefixed_entries[{index}]",
            recognized=False,
        )
    staging_names = _entry_names(staging, "saved plan staging_entries")
    unrecognized_names = _entry_names(
        unrecognized, "saved plan unrecognized_prefixed_entries"
    )
    if any(not is_current_export_staging_name(name) for name in staging_names):
        invalid = next(
            name
            for name in staging_names
            if not is_current_export_staging_name(name)
        )
        raise SnapshotExportStagingCleanupReconcileError(
            f"saved plan staging_entries contains a non-canonical current "
            f"export staging name: {invalid!r}"
        )
    if any(
        is_current_export_staging_name(name)
        or not is_export_staging_prefix_name(name)
        for name in unrecognized_names
    ):
        invalid = next(
            name
            for name in unrecognized_names
            if is_current_export_staging_name(name)
            or not is_export_staging_prefix_name(name)
        )
        raise SnapshotExportStagingCleanupReconcileError(
            f"saved plan unrecognized_prefixed_entries contains a name that "
            f"is not an unrecognized export-staging prefix: {invalid!r}"
        )
    if set(staging_names) & set(unrecognized_names):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan staging_entries overlap unrecognized_prefixed_entries"
        )
    if len(staging) + len(unrecognized) > MAX_PREFIXED_ENTRIES:
        raise SnapshotExportStagingCleanupReconcileError(
            f"saved plan prefixed export-staging entry count exceeds bound "
            f"{MAX_PREFIXED_ENTRIES}"
        )
    staging_count = _require_int(plan.get("staging_count"), "saved plan staging_count")
    unrecognized_count = _require_int(
        plan.get("unrecognized_prefixed_count"),
        "saved plan unrecognized_prefixed_count",
    )
    other_count = _require_int(
        plan.get("other_entry_count"), "saved plan other_entry_count"
    )
    if staging_count != len(staging):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan staging_count must equal the number of staging_entries"
        )
    if unrecognized_count != len(unrecognized):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan unrecognized_prefixed_count must equal the number of "
            "unrecognized_prefixed_entries"
        )
    candidates = _require_canonical_name_list(
        plan.get("deletion_candidates"),
        "saved plan deletion_candidates",
        kind="current",
        maximum=MAX_PREFIXED_ENTRIES,
    )
    candidate_count = _require_int(
        plan.get("deletion_candidate_count"),
        "saved plan deletion_candidate_count",
    )
    if candidate_count != len(candidates):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan deletion_candidate_count must equal the number of "
            "deletion_candidates"
        )
    blocked_entries = plan.get("blocked_entries")
    if not isinstance(blocked_entries, list):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan blocked_entries must be an array"
        )
    blocked_names: List[str] = []
    for item in blocked_entries:
        if (
            not isinstance(item, dict)
            or set(item.keys()) != {"name", "reason"}
            or not isinstance(item.get("name"), str)
            or not item.get("name")
            or not isinstance(item.get("reason"), str)
            or item.get("reason") not in _BLOCKED_REASONS
        ):
            raise SnapshotExportStagingCleanupReconcileError(
                "saved plan blocked_entries must contain exact name/reason "
                "objects with a producer blocking reason"
            )
        if not _is_canonical_direct_name(str(item["name"])):
            raise SnapshotExportStagingCleanupReconcileError(
                f"saved plan blocked entry is not a direct child name: "
                f"{item['name']!r}"
            )
        if not is_export_staging_prefix_name(str(item["name"])):
            raise SnapshotExportStagingCleanupReconcileError(
                f"saved plan blocked entry is not an export-staging prefix "
                f"name: {item['name']!r}"
            )
        blocked_names.append(str(item["name"]))
    if len(set(blocked_names)) != len(blocked_names) or blocked_names != _byte_sort(
        blocked_names
    ):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan blocked_entries must be unique and sorted in raw "
            "filesystem-byte order"
        )
    blocked_count = _require_int(plan.get("blocked_count"), "saved plan blocked_count")
    if blocked_count != len(blocked_entries):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan blocked_count must equal the number of blocked_entries"
        )
    if set(candidates) & set(blocked_names):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan deletion_candidates overlap blocked_entries"
        )
    if set(candidates) | set(blocked_names) != set(staging_names) | set(
        unrecognized_names
    ):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan candidates and blocked entries must partition the "
            "saved prefixed inventory names"
        )
    _require_bool(
        plan.get("ownership_inference"),
        "saved plan ownership_inference",
        expected=False,
    )
    _require_bool(
        plan.get("cleanup_applied"), "saved plan cleanup_applied", expected=False
    )
    _require_bool(
        plan.get("apply_supported"), "saved plan apply_supported", expected=True
    )
    _require_notices(plan.get("notices"), "saved plan notices")
    declared_inventory = _require_revision(
        plan.get("observed_inventory_revision"),
        "saved plan observed_inventory_revision",
    )
    recomputed_inventory = _recompute_inventory_revision(plan)
    if recomputed_inventory != declared_inventory:
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan observed_inventory_revision does not match the "
            "canonical snapshot-export-staging inventory contract"
        )
    reconstructed = {
        "parent": parent,
        "inventory_revision": declared_inventory,
        "staging_entries": staging,
        "staging_count": staging_count,
        "unrecognized_prefixed_entries": unrecognized,
        "unrecognized_prefixed_count": unrecognized_count,
        "other_entry_count": other_count,
    }
    try:
        recomputed_plan = _plan_from_inventory(reconstructed)
    except (
        SnapshotExportStagingCleanupPlanError,
        SnapshotExportStagingError,
        TypeError,
        ValueError,
        KeyError,
    ) as error:
        raise SnapshotExportStagingCleanupReconcileError(str(error)) from error
    if list(recomputed_plan.get("deletion_candidates") or []) != candidates:
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan deletion_candidates do not match classification of "
            "the saved inventory"
        )
    if list(recomputed_plan.get("blocked_entries") or []) != list(blocked_entries):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan blocked_entries do not match classification of "
            "the saved inventory"
        )
    if list(recomputed_plan.get("notices") or []) != list(plan.get("notices") or []):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan notices do not match the schema-2 producer contract"
        )
    declared_revision = _require_revision(
        plan.get("plan_revision"), "saved plan plan_revision"
    )
    try:
        recomputed_revision = plan_revision_of(plan)
    except (
        SnapshotExportStagingCleanupPlanError,
        TypeError,
        ValueError,
        KeyError,
    ) as error:
        raise SnapshotExportStagingCleanupReconcileError(str(error)) from error
    if recomputed_revision != declared_revision:
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan plan_revision does not match the canonical schema-2 "
            "snapshot-export-staging-cleanup-plan contract"
        )
    if recomputed_plan.get("plan_revision") != declared_revision:
        raise SnapshotExportStagingCleanupReconcileError(
            "saved plan plan_revision does not match the recomputed "
            "schema-2 decision-input hash"
        )
    return dict(plan)


def _remaining_from_plan(
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
    return _byte_sort_unique(remaining)


def _validate_apply_outcome(
    result: Mapping[str, Any], planned: Sequence[str]
) -> str:
    ok = _require_bool(result.get("ok"), "saved apply result ok")
    partial = _require_bool(result.get("partial"), "saved apply result partial")
    changed = _require_bool(result.get("changed"), "saved apply result changed")
    filesystem_changed = _require_bool(
        result.get("filesystem_may_have_changed"),
        "saved apply result filesystem_may_have_changed",
    )
    retry = _require_bool(
        result.get("retry_requires_fresh_plan"),
        "saved apply result retry_requires_fresh_plan",
    )
    _require_bool(
        result.get("cleanup_confirmed"),
        "saved apply result cleanup_confirmed",
        expected=True,
    )
    _require_bool(
        result.get("ownership_inference"),
        "saved apply result ownership_inference",
        expected=False,
    )
    deleted = _require_string_list(
        result.get("deleted_staging_entries"),
        "saved apply result deleted_staging_entries",
    )
    deleted_count = _require_int(
        result.get("deleted_count"), "saved apply result deleted_count"
    )
    if deleted_count != len(deleted):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved apply result deleted_count must equal the number of "
            "deleted_staging_entries"
        )
    failed = _require_optional_string(
        result.get("failed_staging_entry"),
        "saved apply result failed_staging_entry",
    )
    not_attempted = _require_string_list(
        result.get("not_attempted_staging_entries"),
        "saved apply result not_attempted_staging_entries",
    )
    remaining = _require_canonical_name_list(
        result.get("remaining_staging_entries"),
        "saved apply result remaining_staging_entries",
        kind="prefixed",
        maximum=MAX_PREFIXED_ENTRIES,
    )
    error = result.get("error")
    if error is not None and (
        not isinstance(error, str)
        or error == ""
        or len(error) > _MAX_ERROR_CHARS
    ):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved apply result error must be a nonempty bounded string or null"
        )
    if ok is not (not partial):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved apply result ok must be the negation of partial"
        )
    if changed is not bool(deleted):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved apply result changed must reflect completed candidate deletions"
        )
    if filesystem_changed is not bool(deleted or partial):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved apply result filesystem_may_have_changed is inconsistent"
        )
    if retry is not bool(partial):
        raise SnapshotExportStagingCleanupReconcileError(
            "saved apply result retry_requires_fresh_plan must match partial"
        )
    planned_list = list(planned)
    if (
        ok is True
        and partial is False
        and deleted == planned_list
        and failed is None
        and not not_attempted
        and retry is False
        and error is None
    ):
        return _DECLARED_COMPLETE
    if (
        ok is False
        and partial is True
        and filesystem_changed is True
        and retry is True
        and isinstance(error, str)
        and error != ""
        and failed is not None
    ):
        index = len(deleted)
        if (
            index < len(planned_list)
            and deleted == planned_list[:index]
            and failed == planned_list[index]
            and not_attempted == planned_list[index + 1 :]
        ):
            return _DECLARED_PARTIAL
    raise SnapshotExportStagingCleanupReconcileError(
        "saved apply result is not an exact complete or partial producer outcome"
    )


def _validate_saved_apply_result(
    result: Mapping[str, Any], plan: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    schema = result.get("schema_version")
    if isinstance(schema, bool) or schema != APPLY_SCHEMA_VERSION:
        raise SnapshotExportStagingCleanupReconcileError(
            "saved apply result schema_version must be 1"
        )
    if set(result.keys()) != _APPLY_KEYS:
        raise SnapshotExportStagingCleanupReconcileError(
            "saved apply result does not have the exact schema-1 producer fields"
        )
    parent = _require_absolute_string(
        result.get("parent"), "saved apply result parent"
    )
    expected = _require_revision(
        result.get("expected_plan_revision"),
        "saved apply result expected_plan_revision",
    )
    observed = _require_revision(
        result.get("observed_plan_revision"),
        "saved apply result observed_plan_revision",
    )
    planned = _require_canonical_name_list(
        result.get("planned_deletion_candidates"),
        "saved apply result planned_deletion_candidates",
        kind="current",
        maximum=MAX_PREFIXED_ENTRIES,
    )
    outcome = _validate_apply_outcome(result, planned)
    _require_notices(result.get("notices"), "saved apply result notices")
    remaining = list(result.get("remaining_staging_entries") or [])
    failed = result.get("failed_staging_entry")
    not_attempted = list(result.get("not_attempted_staging_entries") or [])
    mismatches: List[str] = []
    if parent != plan.get("parent"):
        mismatches.append("parent")
    if expected != plan.get("plan_revision"):
        mismatches.append("expected_plan_revision")
    if observed != plan.get("plan_revision"):
        mismatches.append("observed_plan_revision")
    if planned != list(plan.get("deletion_candidates") or []):
        mismatches.append("planned_deletion_candidates")
    expected_remaining = _remaining_from_plan(
        plan, failed=failed, not_attempted=not_attempted
    )
    if remaining != expected_remaining:
        mismatches.append("remaining_staging_entries")
    if mismatches:
        raise SnapshotExportStagingCleanupReconcileIntegrityError(
            "saved apply result refers to another plan or parent: "
            + ", ".join(mismatches)
        )
    return outcome, dict(result)


def _lookup_entries(inventory: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    for item in _prefixed_entries(inventory):
        name = str(item.get("name") or "")
        if name:
            found[name] = dict(item)
    return found


def _observe_name(
    name: str,
    current: Optional[Mapping[str, Any]],
    saved: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    if current is None:
        return {
            "name": name,
            "state": _STATE_ABSENT,
            "blocking_reason": None,
            "identity_matches_saved_observation": None,
        }
    kind = current.get("kind")
    if kind == "symlink":
        state = _STATE_SYMLINK
        blocking_reason: Optional[str] = "non_directory_staging_entry"
    elif kind != "directory":
        state = _STATE_NON_DIRECTORY
        blocking_reason = "non_directory_staging_entry"
    else:
        classification, reason = _classify_entry(current)
        if classification == "candidate":
            state = _STATE_CANDIDATE
            blocking_reason = None
        else:
            state = _STATE_BLOCKED
            blocking_reason = str(reason)
    saved_identity = _entry_identity(saved) if saved is not None else None
    current_identity = _entry_identity(current)
    identity_matches: Optional[bool]
    if saved_identity is None or current_identity is None:
        identity_matches = False if saved is not None else None
    else:
        identity_matches = saved_identity == current_identity
    return {
        "name": name,
        "state": state,
        "blocking_reason": blocking_reason,
        "identity_matches_saved_observation": identity_matches,
    }


def _discrepancy(code: str, message: str, *, name: str, observed_state: str) -> Dict[str, str]:
    return {
        "code": code,
        "kind": "discrepancy",
        "name": name,
        "message": message,
        "observed_state": observed_state,
    }


def _compare_apply_observation(
    name: str,
    observed: Mapping[str, Any],
    *,
    deleted: Sequence[str],
    remaining: Sequence[str],
) -> Optional[Dict[str, str]]:
    state = str(observed.get("state"))
    if name in deleted and state != _STATE_ABSENT:
        return _discrepancy(
            "declared_deleted_but_present",
            "apply result declared this candidate deleted, but the pathname "
            "is not absent at reconcile",
            name=name,
            observed_state=state,
        )
    if name not in remaining:
        return None
    if state == _STATE_ABSENT:
        return _discrepancy(
            "declared_remaining_but_absent",
            "apply result declared this name remaining, but it is absent "
            "at reconcile",
            name=name,
            observed_state=state,
        )
    if state in {_STATE_NON_DIRECTORY, _STATE_SYMLINK}:
        return _discrepancy(
            "declared_remaining_but_non_directory",
            "apply result declared this name remaining, but it is not a "
            "directory at reconcile",
            name=name,
            observed_state=state,
        )
    if observed.get("identity_matches_saved_observation") is not True:
        return _discrepancy(
            "declared_remaining_but_identity_changed",
            "apply result declared this name remaining, but its observed "
            "identity differs from the saved plan observation",
            name=name,
            observed_state=state,
        )
    return None


def _build_result(
    *,
    plan: Mapping[str, Any],
    inventory: Mapping[str, Any],
    apply_result: Optional[Mapping[str, Any]],
    declared_outcome: str,
) -> Dict[str, Any]:
    current_by_name = _lookup_entries(inventory)
    saved_by_name = _lookup_entries(plan)
    candidates = list(plan.get("deletion_candidates") or [])
    blocked = [
        str(item.get("name") or "")
        for item in (plan.get("blocked_entries") or [])
        if item.get("name")
    ]
    candidate_observations = [
        _observe_name(name, current_by_name.get(name), saved_by_name.get(name))
        for name in candidates
    ]
    blocked_observations = [
        _observe_name(name, current_by_name.get(name), saved_by_name.get(name))
        for name in blocked
    ]
    saved_names = set(saved_by_name)
    new_prefixed_entries = _byte_sort(
        [name for name in current_by_name if name not in saved_names]
    )
    all_absent = all(
        item["state"] == _STATE_ABSENT for item in candidate_observations
    )
    discrepancies: List[Dict[str, str]] = []
    consistent: Optional[bool]
    if apply_result is None:
        consistent = None
    else:
        deleted = list(apply_result.get("deleted_staging_entries") or [])
        remaining = list(apply_result.get("remaining_staging_entries") or [])
        watched = _byte_sort_unique([*candidates, *blocked])
        for name in watched:
            if name in candidates:
                observed = next(
                    item for item in candidate_observations if item["name"] == name
                )
            else:
                observed = next(
                    item for item in blocked_observations if item["name"] == name
                )
            found = _compare_apply_observation(
                name,
                observed,
                deleted=deleted,
                remaining=remaining,
            )
            if found is not None:
                discrepancies.append(found)
        for name in new_prefixed_entries:
            current = current_by_name[name]
            observed = _observe_name(name, current, None)
            discrepancies.append(
                _discrepancy(
                    "new_prefixed_entry",
                    "a prefixed export-staging name is present that was "
                    "absent from the saved plan",
                    name=name,
                    observed_state=str(observed["state"]),
                )
            )
        consistent = not bool(discrepancies)
    return {
        "schema_version": RECONCILE_SCHEMA_VERSION,
        "ok": True,
        "parent": inventory["parent"],
        "input_plan_revision": plan["plan_revision"],
        "input_observed_inventory_revision": plan["observed_inventory_revision"],
        "apply_result_supplied": apply_result is not None,
        "apply_result_valid": True if apply_result is not None else None,
        "declared_apply_outcome": declared_outcome,
        "observed_inventory_revision": inventory["inventory_revision"],
        "candidate_observations": candidate_observations,
        "blocked_observations": blocked_observations,
        "new_prefixed_entries": new_prefixed_entries,
        "all_planned_candidates_absent_at_reconcile": all_absent,
        "result_consistent_with_observation": consistent,
        "discrepancies": discrepancies,
        "reconciliation_is_observation_only": True,
        "deletion_cause_proven": False,
        "recovery_performed": False,
        "fresh_plan_required_before_mutation": True,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }


@contextmanager
def _snapshot_export_staging_cleanup_reconcile_scope(
    parent: object,
    plan_file: Path,
    apply_result_file: Optional[Path] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield one reconcile result while parent and probe descriptors stay held."""
    plan = _validate_saved_plan(_load_json_object(plan_file, label="plan file"))
    apply_result: Optional[Dict[str, Any]] = None
    declared_outcome = _DECLARED_NOT_SUPPLIED
    if apply_result_file is not None:
        _outcome, apply_result = _validate_saved_apply_result(
            _load_json_object(apply_result_file, label="apply-result file"),
            plan,
        )
        declared_outcome = _outcome
    try:
        with export_staging_observation_scope(parent) as inventory:
            if inventory.get("parent") != plan.get("parent"):
                raise SnapshotExportStagingCleanupReconcileIntegrityError(
                    f"requested parent {inventory.get('parent')!r} does not "
                    f"match saved plan parent {plan.get('parent')!r}"
                )
            result = _build_result(
                plan=plan,
                inventory=inventory,
                apply_result=apply_result,
                declared_outcome=declared_outcome,
            )
            _after_result_ready(Path(inventory["parent"]), result)
            yield result
    except SnapshotExportStagingCleanupReconcileError:
        raise
    except SnapshotExportStagingError as error:
        raise _wrap_staging_error(error) from error


def snapshot_export_staging_cleanup_reconcile(
    parent: Path,
    plan_file: Path,
    apply_result_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reconcile a saved export-staging cleanup plan against the live parent.

    Returns the schema-1 observation. ``ok=true`` means this read
    completed. Remaining candidates or discrepancies do not change the
    exit code. Pre-response failures raise
    :class:`SnapshotExportStagingCleanupReconcileError` (exit 2) or
    :class:`SnapshotExportStagingCleanupReconcileIntegrityError` (exit 1).
    """
    with _snapshot_export_staging_cleanup_reconcile_scope(
        parent, plan_file, apply_result_file
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Observe the aftermath of a saved schema-2 "
            "snapshot-export-staging-cleanup-plan. Optional "
            "--apply-result-file compares a saved schema-1 "
            "snapshot-export-staging-cleanup result. Observation-only: no "
            "recovery, mutation, writer claim, graph inspection, or retry "
            "token. A fresh cleanup plan is required before any later "
            "apply. Not an MCP tool. Input files are regular files bounded "
            f"at {MAX_INPUT_BYTES} bytes."
        )
    )
    parser.add_argument(
        "--parent",
        type=Path,
        required=True,
        help="Parent directory to observe, relative to cwd.",
    )
    parser.add_argument(
        "--plan-file",
        type=Path,
        required=True,
        help=(
            "Saved schema-2 snapshot-export-staging-cleanup-plan JSON, "
            "relative to cwd."
        ),
    )
    parser.add_argument(
        "--apply-result-file",
        type=Path,
        default=None,
        help=(
            "Optional saved schema-1 snapshot-export-staging-cleanup JSON, "
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
        with _snapshot_export_staging_cleanup_reconcile_scope(
            args.parent,
            args.plan_file,
            args.apply_result_file,
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            sys.stdout.flush()
            return 0 if result["ok"] else 1
    except SnapshotExportStagingCleanupReconcileError as error:
        print(
            f"snapshot-export-staging-cleanup-reconcile: {error}",
            file=sys.stderr,
        )
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(
            f"snapshot-export-staging-cleanup-reconcile: {error}",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
