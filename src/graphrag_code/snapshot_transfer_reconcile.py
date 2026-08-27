#!/usr/bin/env python
"""Read-only snapshot transfer reconciliation.

``snapshot-transfer-reconcile`` inspects the aftermath of a planned or
attempted direct snapshot transfer. It reconciles one saved schema-1
``snapshot-transfer-plan``, an optional saved schema-1
``snapshot-transfer-apply`` result, and the current states of both
managed graphs. It is observation-only. It does not retry, copy,
recover, publish, activate, delete, clean staging, pin, prune, run
retention, repair, reindex, export, import, or mutate either graph.

Saved plan and apply-result paths are relative to the invoking cwd
unless absolute. Only bounded regular files are accepted; they are
opened read-only without following symlinks. The conservative limit is
``MAX_INPUT_BYTES`` (1 MiB). Complete input validation finishes before
either graph is observed. Malformed, oversized, symlinked, replaced,
truncated, or structurally invalid inputs fail with exit 2 and empty
stdout. A structurally valid apply result that refers to another plan,
graph pair, revision, snapshot, or file contract is an integrity
failure: exit 1 and empty stdout. A saved apply result is impossible
for a blocked saved plan. A saved apply result is only an
unauthenticated declaration being compared with current state.

Both graph arguments may be relative. Each must be an existing real
managed ``current + snapshots/`` graph with an already-existing regular
``.publish.lock``. The command never creates, adopts, writes, chmods,
truncates, or replaces either lock. Same-graph identity, including path
aliases for the same ``(st_dev, st_ino)``, is malformed invocation:
exit 2 and empty stdout, before any nested lease.

One shared existing-lock lease is held on each graph for the complete
joint observation, result construction, serialization, stdout write,
and flush. The two leases are acquired in the established global
transfer order independent of source/target role: canonical UTF-8
graph-root path bytes, then ``(st_dev, st_ino)``. Opposing A→B and B→A
reconciliations therefore cannot deadlock.

Both graph roots and both ``snapshots/`` directories are anchored with
no-follow descriptors and complete identities. Graph, snapshots,
selected-snapshot, and payload descriptors plus both leases stay held
through result construction, serialization, stdout write, and flush.
The command does not invoke public transfer-plan or transfer-apply
scopes and does not create a nested observation window.

``ok=true`` means this read-only reconciliation completed. It does not
mean apply succeeded or that either snapshot matches. A fresh transfer
plan is required before any later apply. MCP stays exactly 14
read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-transfer-reconcile --source-graph <managed-root> \\
        --target-graph <managed-root> --plan-file <saved-transfer-plan.json> \\
        [--apply-result-file <saved-transfer-apply-result.json>] [--json]
    python -m graphrag_code.snapshot_transfer_reconcile --source-graph <managed-root> \\
        --target-graph <managed-root> --plan-file <saved-transfer-plan.json> \\
        [--apply-result-file <saved-transfer-apply-result.json>] [--json]
    uv run python scripts/snapshot_transfer_reconcile.py --source-graph <managed-root> \\
        --target-graph <managed-root> --plan-file <saved-transfer-plan.json> \\
        [--apply-result-file <saved-transfer-apply-result.json>] [--json]
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

from graphrag_code.byog_graph import (
    PUBLICATION_LOCK_NAME,
    STAGING_NAME_PREFIX,
    ByogPublicationLockError,
    ByogReaderLockError,
    graph_shared_leases,
    is_published_snapshot_id,
    is_staging_snapshot_name,
    ordered_graph_lease_pair,
)
from graphrag_code.byog_snapshot_integrity import MANIFEST_NAME
from graphrag_code.snapshot_export_plan import (
    ACCEPTED_PAYLOAD_FILES,
    MAX_MANIFEST_BYTES,
    PLAN_SCHEMA_VERSION,
    REQUIRED_PAYLOAD_FILES,
    SnapshotExportPlanError,
    SnapshotExportPlanIntegrityError,
    _directory_identity,
    _is_canonical_direct_name,
    _require_descriptor_reads,
    canonical_export_revision_payload,
    export_revision_of,
)
from graphrag_code.snapshot_import_plan import (
    SnapshotImportPlanError,
    SnapshotImportPlanIntegrityError,
    _complete_directory_identity,
    _complete_file_identity,
    _observe_held_directory,
    _observe_listing,
    _open_held_payload,
    _parse_manifest,
    _read_held_bytes,
    _reobserve_held_payload,
    _validate_source_envelope,
)
from graphrag_code.snapshot_read import CURRENT_REF
from graphrag_code.snapshot_staging import (
    MAX_CURRENT_BYTES,
    MAX_PUBLISHED_SNAPSHOTS,
    MAX_STAGING_ENTRIES,
    SnapshotStagingError,
    SnapshotStagingIntegrityError,
)
from graphrag_code.snapshot_transfer_plan import (
    SnapshotTransferPlanError,
    SnapshotTransferPlanIntegrityError,
    _open_anchored_directory as _plan_open_anchored_directory,
    _reject_same_graph as _plan_reject_same_graph,
    _require_managed_graph as _plan_require_managed_graph,
    _require_path_identity as _plan_require_path_identity,
    _resolve_existing_real_directory as _plan_resolve_existing_real_directory,
    canonical_transfer_revision_payload,
    transfer_revision_of,
)


RECONCILE_SCHEMA_VERSION = 1
PLAN_SCHEMA_VERSION_TRANSFER = 1
APPLY_SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1 * 1024 * 1024
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_STATE_ABSENT = "absent"
_STATE_MATCHES = "matches_plan"
_STATE_MISMATCH = "revision_mismatch"
_DECLARED_NOT_SUPPLIED = "not_supplied"
_DECLARED_COMPLETE = "complete"
_DECLARED_PRE_PUBLICATION = "pre_publication_partial"
_DECLARED_POST_PUBLICATION = "post_publication_partial"
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
            "snapshot-transfer-reconcile is observation-only. ok means this "
            "read completed, not that snapshot-transfer-apply succeeded, that "
            "either snapshot exists, that it matches, that staging was "
            "cleaned, or that current stayed unchanged."
        ),
    },
    {
        "code": "source_absence_does_not_prove_apply_modified_source",
        "kind": "notice",
        "message": (
            "Source snapshot absence does not prove snapshot-transfer-apply "
            "modified or deleted it. Transfer apply never mutates the source."
        ),
    },
    {
        "code": "target_absence_does_not_prove_apply_failed",
        "kind": "notice",
        "message": (
            "Target snapshot absence does not prove snapshot-transfer-apply "
            "failed or that another actor did not delete it."
        ),
    },
    {
        "code": "target_presence_does_not_prove_apply_created",
        "kind": "notice",
        "message": (
            "Target snapshot presence does not prove snapshot-transfer-apply "
            "created it. transfer_cause_proven is always false."
        ),
    },
    {
        "code": "revision_equality_is_observation_window_only",
        "kind": "notice",
        "message": (
            "Matching revision proves only equality with the saved payload "
            "contract during the bounded observation window."
        ),
    },
    {
        "code": "staging_presence_does_not_prove_apply_left_it",
        "kind": "notice",
        "message": (
            "Exact staging presence does not prove snapshot-transfer-apply "
            "left it. staging_cause_proven is always false."
        ),
    },
    {
        "code": "staging_absence_does_not_prove_apply_cleaned_it",
        "kind": "notice",
        "message": (
            "Exact staging absence does not prove snapshot-transfer-apply "
            "cleaned it."
        ),
    },
    {
        "code": "saved_apply_result_is_declaration_only",
        "kind": "notice",
        "message": (
            "A saved complete or partial apply result is a declaration being "
            "compared, not independently authenticated provenance or proof of "
            "what created or removed anything."
        ),
    },
    {
        "code": "current_equality_is_not_activation_history",
        "kind": "notice",
        "message": (
            "Current equality with the saved plan does not prove activation "
            "or non-activation history."
        ),
    },
    {
        "code": "no_recovery_performed",
        "kind": "notice",
        "message": (
            "recovery_performed is always false. Reconciliation performs no "
            "retry, recovery, restore, activation, deletion, cleanup, "
            "repair, pinning, pruning, or retention, and authorizes no "
            "cleanup or deletion."
        ),
    },
    {
        "code": "fresh_plan_required_before_transfer",
        "kind": "notice",
        "message": (
            "A fresh transfer plan is required before any later apply. This "
            "reconciliation is not a retry token."
        ),
    },
    {
        "code": "not_backup_authenticity_or_provenance",
        "kind": "notice",
        "message": (
            "This is not backup, authenticity, provenance, portability, "
            "recoverability, restore success, or semantic-equivalence "
            "evidence."
        ),
    },
    {
        "code": "advisory_locks_cooperating_only",
        "kind": "notice",
        "message": (
            "Advisory locks protect only cooperating processes. This is not "
            "continuous protection against lock-ignoring actors."
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
            "snapshot-transfer-reconcile is CLI-only and intentionally "
            "absent from the fixed 14-tool MCP set."
        ),
    },
)


class SnapshotTransferReconcileError(Exception):
    """Malformed arguments, inputs, or unsupported invocation. Default exit 2."""

    exit_code = 2


class SnapshotTransferReconcileIntegrityError(SnapshotTransferReconcileError):
    """Unsafe structure, invalid envelope, mismatch, or concurrent change. Exit 1."""

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
    source_observed = result.get("observed_source_export_revision")
    target_observed = result.get("observed_target_export_revision")
    source_text = "null" if source_observed is None else str(source_observed)
    target_text = "null" if target_observed is None else str(target_observed)
    return (
        "snapshot-transfer-reconcile: "
        f"source_graph={result.get('source_graph')} "
        f"target_graph={result.get('target_graph')} "
        f"snapshot_id={result.get('snapshot_id')} "
        f"source_snapshot_state={result.get('source_snapshot_state')} "
        f"target_snapshot_state={result.get('target_snapshot_state')} "
        f"input_transfer_revision={result.get('input_transfer_revision')} "
        f"observed_source_export_revision={source_text} "
        f"observed_target_export_revision={target_text} "
        "source_snapshot_matches_plan="
        f"{str(bool(result.get('source_snapshot_matches_plan'))).lower()} "
        "target_snapshot_matches_plan="
        f"{str(bool(result.get('target_snapshot_matches_plan'))).lower()} "
        "target_staging_present="
        f"{str(bool(result.get('target_staging_present'))).lower()} "
        f"declared_apply_outcome={result.get('declared_apply_outcome')} "
        f"ok={str(bool(result.get('ok'))).lower()} "
        "This reconciliation is observation-only and is not authorization "
        "to transfer, activate, or delete anything."
    )


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def _inode(identity: Tuple[int, ...]) -> Tuple[int, int]:
    return (int(identity[0]), int(identity[1]))


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant {value!r}")


def _require_revision(value: object, label: str) -> str:
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise SnapshotTransferReconcileError(
            f"{label} must be sha256:<64 lowercase hex>, got {value!r}"
        )
    return value


def _require_bool(
    value: object, label: str, *, expected: Optional[bool] = None
) -> bool:
    if type(value) is not bool:
        raise SnapshotTransferReconcileError(f"{label} must be a JSON boolean")
    if expected is not None and value is not expected:
        raise SnapshotTransferReconcileError(
            f"{label} must be {str(expected).lower()}"
        )
    return value


def _require_non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotTransferReconcileError(f"{label} must be a non-empty string")
    return value


def _require_absolute_string(value: object, label: str) -> str:
    text = _require_non_empty_string(value, label)
    if not Path(text).is_absolute():
        raise SnapshotTransferReconcileError(f"{label} must be an absolute path")
    return text


def _require_canonical_snapshot_id(value: object, label: str) -> str:
    text = _require_non_empty_string(value, label)
    if not _is_canonical_direct_name(text) or not is_published_snapshot_id(text):
        raise SnapshotTransferReconcileError(
            f"{label} is not a canonical published snapshot id"
        )
    return text


def _require_int(value: object, label: str, *, expected: Optional[int] = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotTransferReconcileError(f"{label} must be a JSON integer")
    if expected is not None and value != expected:
        raise SnapshotTransferReconcileError(
            f"{label} must equal {expected}"
        )
    return value


def _optional_current_after(value: object, label: str) -> Optional[str]:
    if value is None:
        return None
    return _require_canonical_snapshot_id(value, label)


def _after_input_path_lstat(_path: Path) -> None:
    """Test hook after lstat and before the no-follow input open."""
    return None


def _after_input_file_read(_path: Path, _digest: str) -> None:
    """Test hook after the first complete input read and before the recheck."""
    return None


def _after_source_graph_path_inspected(_source: Path) -> None:
    """Test hook after initial source-path validation."""
    return None


def _after_target_graph_path_inspected(_target: Path) -> None:
    """Test hook after initial target-path validation."""
    return None


def _after_graphs_identified(_source: Path, _target: Path) -> None:
    """Test hook after same-graph rejection and before nested leases."""
    return None


def _after_snapshots_path_inspected(_graph_path: Path, _graph_fd: int) -> None:
    """Test hook after snapshots lstat and before the snapshots open."""
    return None


def _after_first_joint_scan(
    _source: Path,
    _target: Path,
    _source_scan: Mapping[str, Any],
    _target_scan: Mapping[str, Any],
) -> None:
    """Test hook after the first joint graph scan."""
    return None


def _after_second_joint_scan(
    _source: Path,
    _target: Path,
    _source_scan: Mapping[str, Any],
    _target_scan: Mapping[str, Any],
) -> None:
    """Test hook after the second joint graph scan and before payload recheck."""
    return None


def _after_source_snapshot_first_stat(
    _path: Path, _info: Optional[os.stat_result]
) -> None:
    """Test hook after the first descriptor-relative source snapshot stat."""
    return None


def _after_target_snapshot_first_stat(
    _path: Path, _info: Optional[os.stat_result]
) -> None:
    """Test hook after the first descriptor-relative target snapshot stat."""
    return None


def _after_target_staging_first_stat(
    _path: Path, _info: Optional[os.stat_result]
) -> None:
    """Test hook after the first descriptor-relative exact staging stat."""
    return None


def _after_source_snapshot_opened(
    _path: Path, _directory_fd: int, _identity: Tuple[int, int, int, int, int]
) -> None:
    """Test hook after the source snapshot directory is anchored."""
    return None


def _after_target_snapshot_opened(
    _path: Path, _directory_fd: int, _identity: Tuple[int, int, int, int, int]
) -> None:
    """Test hook after the target snapshot directory is anchored."""
    return None


def _after_first_source_snapshot_observation(
    _path: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after the first complete source-snapshot observation."""
    return None


def _after_first_target_snapshot_observation(
    _path: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after the first complete target-snapshot observation."""
    return None


def _after_second_listed(
    _path: Path, _entries: Mapping[str, os.stat_result]
) -> None:
    """Test hook after the second-pass snapshot listing."""
    return None


def _after_payload_final_recheck(_path: Path, _name: str) -> None:
    """Test hook after one payload in the final held-hash pass."""
    return None


def _after_result_ready(
    _source: Path,
    _target: Path,
    _source_fd: int,
    _target_fd: int,
    _source_snapshots_fd: int,
    _target_snapshots_fd: int,
    _source_snapshot_fd: Optional[int],
    _target_snapshot_fd: Optional[int],
    _source_payload_fds: Mapping[str, int],
    _target_payload_fds: Mapping[str, int],
    _result: Mapping[str, Any],
) -> None:
    """Test hook after the result is built and before it is yielded."""
    return None


def _wrap_plan_error(error: Exception) -> SnapshotTransferReconcileError:
    if isinstance(error, SnapshotTransferPlanIntegrityError):
        return SnapshotTransferReconcileIntegrityError(str(error))
    if isinstance(error, SnapshotTransferPlanError):
        wrapped = SnapshotTransferReconcileError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    if isinstance(error, SnapshotImportPlanIntegrityError):
        return SnapshotTransferReconcileIntegrityError(str(error))
    if isinstance(error, SnapshotImportPlanError):
        wrapped = SnapshotTransferReconcileError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    if isinstance(error, SnapshotExportPlanIntegrityError):
        return SnapshotTransferReconcileIntegrityError(str(error))
    if isinstance(error, SnapshotExportPlanError):
        wrapped = SnapshotTransferReconcileError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    if isinstance(error, SnapshotStagingIntegrityError):
        return SnapshotTransferReconcileIntegrityError(str(error))
    if isinstance(error, SnapshotStagingError):
        message = str(error)
        lowered = message.lower()
        wrapped = SnapshotTransferReconcileError(message)
        if "exceeds bound" in lowered or "unsupported" in lowered:
            wrapped.exit_code = getattr(error, "exit_code", 2)
        else:
            return SnapshotTransferReconcileIntegrityError(message)
        return wrapped
    return SnapshotTransferReconcileError(str(error))


def _lock_error(error: Exception) -> SnapshotTransferReconcileError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotTransferReconcileError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotTransferReconcileIntegrityError(message)
    return SnapshotTransferReconcileError(message)


def _require_managed_graph(root: Path) -> None:
    try:
        _plan_require_managed_graph(root)
    except SnapshotTransferPlanError as error:
        raise _wrap_plan_error(error) from error


def _resolve_existing_real_directory(
    path: Path, *, label: str
) -> Tuple[Path, Tuple[int, int, int, int, int]]:
    try:
        return _plan_resolve_existing_real_directory(path, label=label)
    except SnapshotTransferPlanError as error:
        raise _wrap_plan_error(error) from error


def _require_path_identity(
    path: Path,
    expected_identity: Tuple[int, int, int, int, int],
    *,
    label: str,
) -> None:
    try:
        _plan_require_path_identity(path, expected_identity, label=label)
    except SnapshotTransferPlanError as error:
        raise _wrap_plan_error(error) from error


def _reject_same_graph(
    source: Path,
    source_identity: Tuple[int, ...],
    target: Path,
    target_identity: Tuple[int, ...],
) -> None:
    try:
        _plan_reject_same_graph(source, source_identity, target, target_identity)
    except SnapshotTransferPlanError as error:
        raise _wrap_plan_error(error) from error


def _open_anchored_directory(
    path: Path,
    expected_path_identity: Tuple[int, int, int, int, int],
    *,
    label: str,
) -> Tuple[Path, int, Tuple[int, int, int, int, int], Tuple[int, int, int, int]]:
    try:
        return _plan_open_anchored_directory(
            path, expected_path_identity, label=label
        )
    except SnapshotTransferPlanError as error:
        raise _wrap_plan_error(error) from error


def _require_existing_regular_lock(root: Path) -> None:
    path = root / PUBLICATION_LOCK_NAME
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotTransferReconcileError(
            f"publication lock is missing; refusing to reconcile "
            f"an unleased managed graph: {path}\n{_MISSING_LOCK_HINT}"
        ) from error
    except OSError as error:
        raise SnapshotTransferReconcileError(
            f"cannot inspect publication lock {path}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotTransferReconcileError(
            f"publication lock must be a regular file, not a symlink: {path}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotTransferReconcileError(
            f"publication lock is not a regular file: {path}"
        )


def _complete_input_identity(
    info: os.stat_result,
) -> Tuple[int, int, int, int, int, int]:
    return _complete_file_identity(info)


def _hash_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_bounded_regular_file(path: Path, *, label: str) -> bytes:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    try:
        before = resolved.lstat()
    except FileNotFoundError as error:
        raise SnapshotTransferReconcileError(
            f"{label} does not exist: {resolved}"
        ) from error
    except OSError as error:
        raise SnapshotTransferReconcileError(
            f"cannot inspect {label} {resolved}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotTransferReconcileError(
            f"{label} must be a regular file, not a symlink: {resolved}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotTransferReconcileError(
            f"{label} is not a regular file: {resolved}"
        )
    if before.st_size > MAX_INPUT_BYTES:
        raise SnapshotTransferReconcileError(
            f"{label} exceeds bound {MAX_INPUT_BYTES} bytes: {resolved}"
        )
    if not hasattr(os, "O_NOFOLLOW"):
        raise SnapshotTransferReconcileError(
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
            raise SnapshotTransferReconcileError(
                f"{label} changed or became unsafe while opening it: {resolved}"
            ) from error
        raise SnapshotTransferReconcileError(
            f"cannot safely open {label} {resolved}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = resolved.lstat()
        except OSError as error:
            raise SnapshotTransferReconcileError(
                f"{label} changed while opening it: {resolved}"
            ) from error
        before_id = _complete_input_identity(before)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _complete_input_identity(opened) != before_id
            or _complete_input_identity(current) != before_id
        ):
            raise SnapshotTransferReconcileError(
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
            raise SnapshotTransferReconcileError(
                f"{label} exceeds bound {MAX_INPUT_BYTES} bytes: {resolved}"
            )
        after_fd = os.fstat(fd)
        try:
            after_path = resolved.lstat()
        except OSError as error:
            raise SnapshotTransferReconcileError(
                f"{label} changed while it was read: {resolved}"
            ) from error
        if (
            _complete_input_identity(after_fd) != before_id
            or stat.S_ISLNK(after_path.st_mode)
            or not stat.S_ISREG(after_path.st_mode)
            or _complete_input_identity(after_path) != before_id
            or len(data) != opened.st_size
        ):
            raise SnapshotTransferReconcileError(
                f"{label} changed while it was read: {resolved}"
            )
        digest = _hash_bytes(data)
        _after_input_file_read(resolved, digest)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError as error:
            raise SnapshotTransferReconcileError(
                f"cannot rewind {label} {resolved}: {error}"
            ) from error
        second_chunks: List[bytes] = []
        second_total = 0
        while second_total <= MAX_INPUT_BYTES:
            chunk = os.read(fd, min(8192, MAX_INPUT_BYTES + 1 - second_total))
            if not chunk:
                break
            second_chunks.append(chunk)
            second_total += len(chunk)
        second = b"".join(second_chunks)
        final_fd = os.fstat(fd)
        try:
            final_path = resolved.lstat()
        except OSError as error:
            raise SnapshotTransferReconcileError(
                f"{label} changed while it was read: {resolved}"
            ) from error
        if (
            second != data
            or _hash_bytes(second) != digest
            or _complete_input_identity(final_fd) != before_id
            or stat.S_ISLNK(final_path.st_mode)
            or not stat.S_ISREG(final_path.st_mode)
            or _complete_input_identity(final_path) != before_id
        ):
            raise SnapshotTransferReconcileError(
                f"{label} changed while it was read: {resolved}"
            )
    finally:
        os.close(fd)
    return data


def _load_json_object(path: Path, *, label: str) -> Tuple[Path, Dict[str, Any]]:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    data = _read_bounded_regular_file(resolved, label=label)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SnapshotTransferReconcileError(
            f"{label} is not valid UTF-8: {resolved}"
        ) from error

    def unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate JSON object key {key!r}")
            out[key] = value
        return out

    try:
        parsed = json.loads(
            text,
            parse_constant=_reject_nonstandard_json_constant,
            object_pairs_hook=unique_object,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise SnapshotTransferReconcileError(
            f"{label} is not valid JSON: {error}"
        ) from error
    if not isinstance(parsed, dict):
        raise SnapshotTransferReconcileError(f"{label} must be a JSON object")
    return resolved, parsed


def _file_records(value: object, *, label: str, snapshot_id: str) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise SnapshotTransferReconcileError(f"{label} must be an array")
    try:
        canonical = canonical_export_revision_payload(
            {
                "schema_version": PLAN_SCHEMA_VERSION,
                "resolved_snapshot": snapshot_id,
                "files": value,
            }
        )
    except SnapshotExportPlanError as error:
        raise SnapshotTransferReconcileError(f"{label}: {error}") from error
    records = [
        {
            "path": item["path"],
            "size_bytes": item["size_bytes"],
            "content_revision": item["content_revision"],
        }
        for item in canonical["files"]
    ]
    names = [str(item["path"]) for item in records]
    missing = [name for name in _byte_sort(REQUIRED_PAYLOAD_FILES) if name not in names]
    if missing:
        raise SnapshotTransferReconcileError(
            f"{label} is missing required envelope payload {missing[0]}"
        )
    extra = [name for name in names if name not in ACCEPTED_PAYLOAD_FILES]
    if extra:
        raise SnapshotTransferReconcileError(
            f"{label} contains an unexpected envelope name: {extra[0]!r}"
        )
    if list(names) != _byte_sort(names):
        raise SnapshotTransferReconcileError(
            f"{label} must be sorted in UTF-8-byte order"
        )
    return records


def _require_published_history(
    value: object, *, label: str, current: str, current_label: str
) -> List[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SnapshotTransferReconcileError(
            f"{label} must be a list of snapshot ids"
        )
    published_ids = list(value)
    if any(not is_published_snapshot_id(item) for item in published_ids):
        raise SnapshotTransferReconcileError(
            f"{label} contains a non-canonical id"
        )
    if len(set(published_ids)) != len(published_ids) or published_ids != _byte_sort(
        published_ids
    ):
        raise SnapshotTransferReconcileError(
            f"{label} must be unique and sorted in UTF-8-byte order"
        )
    if current not in published_ids:
        raise SnapshotTransferReconcileError(
            f"{current_label} is not a member of {label}"
        )
    if len(published_ids) > MAX_PUBLISHED_SNAPSHOTS:
        raise SnapshotTransferReconcileError(
            f"saved plan published snapshot count exceeds bound "
            f"{MAX_PUBLISHED_SNAPSHOTS}"
        )
    return published_ids


def _validate_saved_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    schema = plan.get("schema_version")
    if isinstance(schema, bool) or schema != PLAN_SCHEMA_VERSION_TRANSFER:
        raise SnapshotTransferReconcileError("saved plan schema_version must be 1")
    _require_bool(plan.get("ok"), "saved plan ok", expected=True)
    source_graph = _require_absolute_string(
        plan.get("source_graph"), "saved plan source_graph"
    )
    target_graph = _require_absolute_string(
        plan.get("target_graph"), "saved plan target_graph"
    )
    requested = _require_non_empty_string(
        plan.get("requested_snapshot"), "saved plan requested_snapshot"
    )
    snapshot_id = _require_canonical_snapshot_id(
        plan.get("snapshot_id"), "saved plan snapshot_id"
    )
    if requested != CURRENT_REF and requested != snapshot_id:
        raise SnapshotTransferReconcileError(
            "saved plan requested_snapshot must be current or equal snapshot_id"
        )
    if requested != CURRENT_REF:
        if not _is_canonical_direct_name(requested) or not is_published_snapshot_id(
            requested
        ):
            raise SnapshotTransferReconcileError(
                "saved plan requested_snapshot is not a canonical published id"
            )
    source_current = _require_canonical_snapshot_id(
        plan.get("source_current"), "saved plan source_current"
    )
    source_published = _require_published_history(
        plan.get("source_published_snapshots"),
        label="saved plan source_published_snapshots",
        current=source_current,
        current_label="saved plan source_current",
    )
    if snapshot_id not in set(source_published):
        raise SnapshotTransferReconcileError(
            "saved plan snapshot_id is not a member of source_published_snapshots"
        )
    records = _file_records(
        plan.get("files"), label="saved plan files", snapshot_id=snapshot_id
    )
    _require_int(plan.get("file_count"), "saved plan file_count", expected=len(records))
    expected_total = sum(int(item["size_bytes"]) for item in records)
    _require_int(
        plan.get("total_size_bytes"),
        "saved plan total_size_bytes",
        expected=expected_total,
    )
    _require_bool(
        plan.get("source_envelope_valid"),
        "saved plan source_envelope_valid",
        expected=True,
    )
    target_current = _require_canonical_snapshot_id(
        plan.get("target_current"), "saved plan target_current"
    )
    target_published = _require_published_history(
        plan.get("target_published_snapshots"),
        label="saved plan target_published_snapshots",
        current=target_current,
        current_label="saved plan target_current",
    )
    _require_int(
        plan.get("target_published_count"),
        "saved plan target_published_count",
        expected=len(target_published),
    )
    staging_name = plan.get("target_staging_name")
    expected_staging = f"{STAGING_NAME_PREFIX}{snapshot_id}"
    if (
        not isinstance(staging_name, str)
        or not _is_canonical_direct_name(staging_name)
        or staging_name != expected_staging
    ):
        raise SnapshotTransferReconcileError(
            "saved plan target_staging_name must be exactly .staging-<snapshot-id>"
        )
    staging_count = plan.get("target_staging_count")
    if (
        isinstance(staging_count, bool)
        or not isinstance(staging_count, int)
        or staging_count < 0
        or staging_count > MAX_STAGING_ENTRIES
    ):
        raise SnapshotTransferReconcileError(
            "saved plan target_staging_count must be a non-negative integer no "
            f"greater than {MAX_STAGING_ENTRIES}"
        )
    target_staging_present = _require_bool(
        plan.get("target_staging_present"), "saved plan target_staging_present"
    )
    if target_staging_present is True and staging_count < 1:
        raise SnapshotTransferReconcileError(
            "saved plan target_staging_count is inconsistent with "
            "target_staging_present"
        )
    _require_bool(
        plan.get("transfer_performed"), "saved plan transfer_performed", expected=False
    )
    _require_bool(
        plan.get("source_graph_mutated"),
        "saved plan source_graph_mutated",
        expected=False,
    )
    _require_bool(
        plan.get("target_graph_mutated"),
        "saved plan target_graph_mutated",
        expected=False,
    )
    _require_bool(
        plan.get("fresh_plan_required_before_transfer"),
        "saved plan fresh_plan_required_before_transfer",
        expected=True,
    )
    notices = plan.get("notices")
    if not isinstance(notices, list):
        raise SnapshotTransferReconcileError("saved plan notices must be an array")
    declared_source = _require_revision(
        plan.get("source_export_revision"), "saved plan source_export_revision"
    )
    try:
        recomputed_source = export_revision_of(
            {
                "schema_version": PLAN_SCHEMA_VERSION,
                "resolved_snapshot": snapshot_id,
                "files": records,
            }
        )
    except SnapshotExportPlanError as error:
        raise SnapshotTransferReconcileError(str(error)) from error
    if recomputed_source != declared_source:
        raise SnapshotTransferReconcileError(
            "saved plan source_export_revision does not match the canonical "
            "snapshot-export-plan contract"
        )
    revision_inputs = {
        "schema_version": PLAN_SCHEMA_VERSION_TRANSFER,
        "snapshot_id": snapshot_id,
        "source_export_revision": declared_source,
        "target_current": target_current,
        "target_published_snapshots": target_published,
        "target_staging_name": staging_name,
        "target_snapshot_present": plan.get("target_snapshot_present"),
        "target_staging_present": target_staging_present,
        "blocking_reasons": plan.get("blocking_reasons"),
        "transfer_ready": plan.get("transfer_ready"),
        "source_envelope_valid": True,
        "transfer_performed": False,
        "fresh_plan_required_before_transfer": True,
    }
    try:
        canonical_transfer_revision_payload(revision_inputs)
        recomputed_transfer = transfer_revision_of(revision_inputs)
    except SnapshotTransferPlanError as error:
        raise SnapshotTransferReconcileError(f"saved plan: {error}") from error
    declared_transfer = _require_revision(
        plan.get("transfer_revision"), "saved plan transfer_revision"
    )
    if recomputed_transfer != declared_transfer:
        raise SnapshotTransferReconcileError(
            "saved plan transfer_revision does not match the canonical "
            "snapshot-transfer-plan contract"
        )
    return {
        "schema_version": PLAN_SCHEMA_VERSION_TRANSFER,
        "ok": True,
        "source_graph": source_graph,
        "target_graph": target_graph,
        "requested_snapshot": requested,
        "snapshot_id": snapshot_id,
        "source_current": source_current,
        "source_published_snapshots": source_published,
        "files": records,
        "file_count": len(records),
        "total_size_bytes": expected_total,
        "source_export_revision": declared_source,
        "source_envelope_valid": True,
        "target_current": target_current,
        "target_published_snapshots": target_published,
        "target_published_count": len(target_published),
        "target_staging_name": staging_name,
        "target_snapshot_present": bool(revision_inputs["target_snapshot_present"]),
        "target_staging_present": target_staging_present,
        "target_staging_count": staging_count,
        "blocking_reasons": list(revision_inputs["blocking_reasons"]),
        "transfer_ready": bool(revision_inputs["transfer_ready"]),
        "transfer_revision": declared_transfer,
        "transfer_performed": False,
        "source_graph_mutated": False,
        "target_graph_mutated": False,
        "fresh_plan_required_before_transfer": True,
        "notices": notices,
    }


def _validate_apply_outcome(result: Mapping[str, Any]) -> str:
    ok = _require_bool(result.get("ok"), "saved apply result ok")
    partial = _require_bool(result.get("partial"), "saved apply result partial")
    transfer_confirmed = _require_bool(
        result.get("transfer_confirmed"), "saved apply result transfer_confirmed"
    )
    transfer_performed = _require_bool(
        result.get("transfer_performed"), "saved apply result transfer_performed"
    )
    publication_attempted = _require_bool(
        result.get("publication_attempted"),
        "saved apply result publication_attempted",
    )
    publication_performed = _require_bool(
        result.get("publication_performed"),
        "saved apply result publication_performed",
    )
    snapshot_verified = _require_bool(
        result.get("snapshot_verified_after_publication"),
        "saved apply result snapshot_verified_after_publication",
    )
    source_current_unchanged = _require_bool(
        result.get("source_current_unchanged"),
        "saved apply result source_current_unchanged",
    )
    target_current_unchanged = _require_bool(
        result.get("target_current_unchanged"),
        "saved apply result target_current_unchanged",
    )
    staging_created = _require_bool(
        result.get("staging_created"), "saved apply result staging_created"
    )
    staging_cleanup_attempted = _require_bool(
        result.get("staging_cleanup_attempted"),
        "saved apply result staging_cleanup_attempted",
    )
    staging_remaining = _require_bool(
        result.get("staging_remaining"), "saved apply result staging_remaining"
    )
    snapshots_fsync_confirmed = _require_bool(
        result.get("target_snapshots_fsync_confirmed"),
        "saved apply result target_snapshots_fsync_confirmed",
    )
    filesystem_may_have_changed = _require_bool(
        result.get("filesystem_may_have_changed"),
        "saved apply result filesystem_may_have_changed",
    )
    retry_requires_fresh_plan = _require_bool(
        result.get("retry_requires_fresh_plan"),
        "saved apply result retry_requires_fresh_plan",
        expected=True,
    )
    source_graph_mutated = _require_bool(
        result.get("source_graph_mutated"),
        "saved apply result source_graph_mutated",
        expected=False,
    )
    target_graph_mutated = _require_bool(
        result.get("target_graph_mutated"), "saved apply result target_graph_mutated"
    )
    _require_bool(
        result.get("activation_performed"),
        "saved apply result activation_performed",
        expected=False,
    )
    _require_bool(
        result.get("retention_performed"),
        "saved apply result retention_performed",
        expected=False,
    )
    error = result.get("error")
    if error is not None and (not isinstance(error, str) or error == ""):
        raise SnapshotTransferReconcileError(
            "saved apply result error must be a non-empty string or null"
        )
    source_current_before = _require_canonical_snapshot_id(
        result.get("source_current_before"), "saved apply result source_current_before"
    )
    target_current_before = _require_canonical_snapshot_id(
        result.get("target_current_before"), "saved apply result target_current_before"
    )
    source_current_after = _optional_current_after(
        result.get("source_current_after"), "saved apply result source_current_after"
    )
    target_current_after = _optional_current_after(
        result.get("target_current_after"), "saved apply result target_current_after"
    )
    if source_current_unchanged is not (
        source_current_after == source_current_before
    ):
        raise SnapshotTransferReconcileError(
            "saved apply result source_current_unchanged must exactly report "
            "whether source_current_after equals source_current_before"
        )
    if target_current_unchanged is not (
        target_current_after == target_current_before
    ):
        raise SnapshotTransferReconcileError(
            "saved apply result target_current_unchanged must exactly report "
            "whether target_current_after equals target_current_before"
        )
    complete = (
        ok is True
        and partial is False
        and transfer_confirmed is True
        and transfer_performed is True
        and publication_attempted is True
        and publication_performed is True
        and snapshot_verified is True
        and source_current_after == source_current_before
        and source_current_unchanged is True
        and target_current_after == target_current_before
        and target_current_unchanged is True
        and staging_created is True
        and staging_remaining is False
        and snapshots_fsync_confirmed is True
        and filesystem_may_have_changed is True
        and retry_requires_fresh_plan is True
        and source_graph_mutated is False
        and target_graph_mutated is True
        and staging_cleanup_attempted is False
        and error is None
    )
    pre_publication = (
        ok is False
        and partial is True
        and transfer_confirmed is True
        and staging_created is True
        and transfer_performed is False
        and publication_performed is False
        and snapshot_verified is False
        and source_current_after is None
        and source_current_unchanged is False
        and target_current_after is None
        and target_current_unchanged is False
        and snapshots_fsync_confirmed is False
        and filesystem_may_have_changed is True
        and retry_requires_fresh_plan is True
        and source_graph_mutated is False
        and target_graph_mutated is True
        and isinstance(error, str)
        and error != ""
        and (staging_remaining is False) <= (staging_cleanup_attempted is True)
    )
    post_publication = (
        ok is False
        and partial is True
        and transfer_confirmed is True
        and transfer_performed is True
        and publication_attempted is True
        and publication_performed is True
        and staging_created is True
        and staging_cleanup_attempted is False
        and filesystem_may_have_changed is True
        and retry_requires_fresh_plan is True
        and source_graph_mutated is False
        and target_graph_mutated is True
        and isinstance(error, str)
        and error != ""
        and not (staging_remaining is True and snapshot_verified is True)
    )
    if complete:
        return _DECLARED_COMPLETE
    if pre_publication:
        return _DECLARED_PRE_PUBLICATION
    if post_publication:
        return _DECLARED_POST_PUBLICATION
    raise SnapshotTransferReconcileError(
        "saved apply result flags are not an exact complete-success, "
        "emitted pre-publication partial, or emitted post-publication "
        "partial outcome"
    )


def _validate_saved_apply_result(
    result: Mapping[str, Any], plan: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    schema = result.get("schema_version")
    if isinstance(schema, bool) or schema != APPLY_SCHEMA_VERSION:
        raise SnapshotTransferReconcileError(
            "saved apply result schema_version must be 1"
        )
    outcome = _validate_apply_outcome(result)
    source_graph = _require_absolute_string(
        result.get("source_graph"), "saved apply result source_graph"
    )
    target_graph = _require_absolute_string(
        result.get("target_graph"), "saved apply result target_graph"
    )
    requested = _require_non_empty_string(
        result.get("requested_snapshot"), "saved apply result requested_snapshot"
    )
    snapshot_id = _require_canonical_snapshot_id(
        result.get("snapshot_id"), "saved apply result snapshot_id"
    )
    expected = _require_revision(
        result.get("expected_transfer_revision"),
        "saved apply result expected_transfer_revision",
    )
    observed = _require_revision(
        result.get("observed_transfer_revision"),
        "saved apply result observed_transfer_revision",
    )
    source_export_revision = _require_revision(
        result.get("source_export_revision"),
        "saved apply result source_export_revision",
    )
    records = _file_records(
        result.get("planned_files"),
        label="saved apply result planned_files",
        snapshot_id=snapshot_id,
    )
    file_count = _require_int(
        result.get("file_count"),
        "saved apply result file_count",
        expected=len(records),
    )
    expected_total = sum(int(item["size_bytes"]) for item in records)
    _require_int(
        result.get("total_size_bytes"),
        "saved apply result total_size_bytes",
        expected=expected_total,
    )
    source_current_before = _require_canonical_snapshot_id(
        result.get("source_current_before"), "saved apply result source_current_before"
    )
    target_current_before = _require_canonical_snapshot_id(
        result.get("target_current_before"), "saved apply result target_current_before"
    )
    notices = result.get("notices")
    if not isinstance(notices, list):
        raise SnapshotTransferReconcileError(
            "saved apply result notices must be an array"
        )
    if plan.get("transfer_ready") is not True:
        raise SnapshotTransferReconcileIntegrityError(
            "saved apply result is impossible for a blocked saved plan"
        )
    if (
        source_graph != plan["source_graph"]
        or target_graph != plan["target_graph"]
        or requested != plan["requested_snapshot"]
        or snapshot_id != plan["snapshot_id"]
        or expected != plan["transfer_revision"]
        or observed != plan["transfer_revision"]
        or source_export_revision != plan["source_export_revision"]
        or records != plan["files"]
        or file_count != plan["file_count"]
        or expected_total != plan["total_size_bytes"]
        or source_current_before != plan["source_current"]
        or target_current_before != plan["target_current"]
    ):
        raise SnapshotTransferReconcileIntegrityError(
            "saved apply result refers to another plan"
        )
    return outcome, {
        "schema_version": APPLY_SCHEMA_VERSION,
        "source_graph": source_graph,
        "target_graph": target_graph,
        "requested_snapshot": requested,
        "snapshot_id": snapshot_id,
        "expected_transfer_revision": expected,
        "observed_transfer_revision": observed,
        "source_export_revision": source_export_revision,
        "planned_files": records,
        "file_count": file_count,
        "total_size_bytes": expected_total,
        "source_current_before": source_current_before,
        "target_current_before": target_current_before,
        "declared_apply_outcome": outcome,
        "notices": notices,
    }


def _stat_child(
    directory_fd: int, name: str, path: Path, *, label: str
) -> Optional[os.stat_result]:
    try:
        return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SnapshotTransferReconcileIntegrityError(
            f"cannot inspect {label} {path}: {error}"
        ) from error


def _child_inode(info: os.stat_result) -> Tuple[int, int]:
    return (info.st_dev, info.st_ino)


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
        raise SnapshotTransferReconcileIntegrityError(
            f"{label} changed during reconciliation: {path}"
        ) from error
    if (
        not stat.S_ISDIR(held.st_mode)
        or _child_inode(held) != expected_inode
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _child_inode(current) != expected_inode
    ):
        raise SnapshotTransferReconcileIntegrityError(
            f"{label} changed or no longer names the held directory: {path}"
        )


def _open_child_directory(
    parent_fd: int,
    name: str,
    path: Path,
    child_info: os.stat_result,
    *,
    label: str,
) -> Tuple[int, Tuple[int, int, int, int, int]]:
    if stat.S_ISLNK(child_info.st_mode):
        raise SnapshotTransferReconcileIntegrityError(
            f"{label} must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(child_info.st_mode):
        raise SnapshotTransferReconcileIntegrityError(
            f"{label} is not a real directory: {path}"
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise SnapshotTransferReconcileIntegrityError(
                f"{label} changed or became unsafe while opening it: {path}"
            ) from error
        raise SnapshotTransferReconcileIntegrityError(
            f"cannot safely open {label} {path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        opened_identity = _complete_directory_identity(opened)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or opened_identity != _complete_directory_identity(child_info)
            or _complete_directory_identity(current) != opened_identity
        ):
            raise SnapshotTransferReconcileIntegrityError(
                f"{label} changed or became unsafe while opening it: {path}"
            )
    except Exception:
        os.close(fd)
        raise
    return fd, opened_identity


def _open_anchored_snapshots(
    graph_path: Path,
    graph_fd: int,
    graph_identity: Tuple[int, int, int, int, int],
) -> Tuple[Path, int, Tuple[int, int, int, int, int]]:
    name = "snapshots"
    path = graph_path / name
    try:
        before = os.stat(name, dir_fd=graph_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise SnapshotTransferReconcileError(
            f"snapshots directory does not exist: {path}"
        ) from error
    except OSError as error:
        raise SnapshotTransferReconcileError(
            f"cannot inspect snapshots directory {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotTransferReconcileIntegrityError(
            f"snapshots directory must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotTransferReconcileIntegrityError(
            f"snapshots directory is not a real directory: {path}"
        )
    before_identity = _complete_directory_identity(before)
    _after_snapshots_path_inspected(graph_path, graph_fd)
    fd, opened_identity = _open_child_directory(
        graph_fd, name, path, before, label="snapshots directory"
    )
    try:
        if opened_identity != before_identity:
            raise SnapshotTransferReconcileIntegrityError(
                f"snapshots directory changed or became unsafe while opening it: {path}"
            )
        try:
            _observe_held_directory(
                graph_path, graph_fd, graph_identity, label="graph root"
            )
        except SnapshotImportPlanError as error:
            raise _wrap_plan_error(error) from error
        path_info = path.lstat()
        if (
            stat.S_ISLNK(path_info.st_mode)
            or not stat.S_ISDIR(path_info.st_mode)
            or _complete_directory_identity(path_info) != opened_identity
        ):
            raise SnapshotTransferReconcileIntegrityError(
                f"snapshots directory changed or was replaced: {path}"
            )
        return path, fd, opened_identity
    except Exception:
        os.close(fd)
        raise


def _held_lock_identity(
    graph_path: Path, graph_fd: int
) -> Tuple[int, int, int, int, int, int]:
    path = graph_path / PUBLICATION_LOCK_NAME
    try:
        info = os.stat(PUBLICATION_LOCK_NAME, dir_fd=graph_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise SnapshotTransferReconcileIntegrityError(
            f"publication lock disappeared during reconciliation: {path}"
        ) from error
    except OSError as error:
        raise SnapshotTransferReconcileIntegrityError(
            f"cannot inspect publication lock {path}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotTransferReconcileIntegrityError(
            f"unsafe symlinked publication lock is unsupported: {path}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotTransferReconcileIntegrityError(
            f"publication lock is not a regular file: {path}"
        )
    return _complete_file_identity(info)


def _read_held_current(
    graph_path: Path, graph_fd: int
) -> Tuple[str, Tuple[int, int, int, int, int, int]]:
    path = graph_path / "current"
    try:
        before = os.stat("current", dir_fd=graph_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise SnapshotTransferReconcileIntegrityError(
            f"current pointer disappeared during reconciliation: {path}"
        ) from error
    except OSError as error:
        raise SnapshotTransferReconcileIntegrityError(
            f"cannot inspect current pointer {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotTransferReconcileIntegrityError(
            f"unsafe symlinked current pointer is unsupported: {path}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotTransferReconcileIntegrityError(
            f"current pointer is not a regular file: {path}"
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open("current", flags, dir_fd=graph_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT}:
            raise SnapshotTransferReconcileIntegrityError(
                f"current pointer changed or became unsafe while opening it: {path}"
            ) from error
        raise SnapshotTransferReconcileError(
            f"cannot safely open current pointer {path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        current_path = os.stat("current", dir_fd=graph_fd, follow_symlinks=False)
        before_id = _complete_file_identity(before)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current_path.st_mode)
            or not stat.S_ISREG(current_path.st_mode)
            or _complete_file_identity(opened) != before_id
            or _complete_file_identity(current_path) != before_id
        ):
            raise SnapshotTransferReconcileIntegrityError(
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
            raise SnapshotTransferReconcileError(
                f"current pointer exceeds bound {MAX_CURRENT_BYTES} bytes: {path}"
            )
        after_fd = os.fstat(fd)
        after_path = os.stat("current", dir_fd=graph_fd, follow_symlinks=False)
        if (
            _complete_file_identity(after_fd) != before_id
            or stat.S_ISLNK(after_path.st_mode)
            or not stat.S_ISREG(after_path.st_mode)
            or _complete_file_identity(after_path) != before_id
            or len(data) != opened.st_size
        ):
            raise SnapshotTransferReconcileIntegrityError(
                f"current pointer changed while it was read: {path}"
            )
    finally:
        os.close(fd)
    try:
        current_id = data.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise SnapshotTransferReconcileError(
            f"current pointer is not valid UTF-8: {path}"
        ) from error
    if not is_published_snapshot_id(current_id):
        raise SnapshotTransferReconcileIntegrityError(
            f"current snapshot id is not a published id: {current_id!r}"
        )
    return current_id, before_id


def _list_held_snapshots(
    snapshots_path: Path,
    snapshots_fd: int,
    snapshots_identity: Tuple[int, int, int, int, int],
    snapshot_id: str,
    staging_name: str,
) -> Dict[str, Any]:
    try:
        held = os.fstat(snapshots_fd)
    except OSError as error:
        raise SnapshotTransferReconcileIntegrityError(
            f"cannot inspect snapshots directory descriptor {snapshots_path}: {error}"
        ) from error
    expected_inode = (snapshots_identity[0], snapshots_identity[1])
    if not stat.S_ISDIR(held.st_mode) or _child_inode(held) != expected_inode:
        raise SnapshotTransferReconcileIntegrityError(
            "snapshots directory descriptor changed during reconciliation"
        )
    entries: List[Tuple[str, os.stat_result]] = []
    try:
        with os.scandir(snapshots_fd) as iterator:
            for entry in iterator:
                if len(entries) >= MAX_PUBLISHED_SNAPSHOTS + MAX_STAGING_ENTRIES:
                    raise SnapshotTransferReconcileError(
                        "snapshots directory entry count exceeds bound "
                        f"{MAX_PUBLISHED_SNAPSHOTS + MAX_STAGING_ENTRIES}: "
                        f"{snapshots_path}"
                    )
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise SnapshotTransferReconcileIntegrityError(
                        f"cannot inspect snapshots child {snapshots_path / entry.name}: "
                        f"{error}"
                    ) from error
                entries.append((entry.name, info))
    except SnapshotTransferReconcileError:
        raise
    except OSError as error:
        raise SnapshotTransferReconcileIntegrityError(
            f"cannot list snapshots directory {snapshots_path}: {error}"
        ) from error
    after = os.fstat(snapshots_fd)
    if not stat.S_ISDIR(after.st_mode) or _child_inode(after) != expected_inode:
        raise SnapshotTransferReconcileIntegrityError(
            "snapshots directory changed while it was listed"
        )
    entries.sort(key=lambda item: item[0].encode("utf-8"))
    published: List[str] = []
    published_identities: Dict[str, Tuple[int, int]] = {}
    selected_snapshot: Optional[os.stat_result] = None
    selected_staging: Optional[os.stat_result] = None
    for name, info in entries:
        path = snapshots_path / name
        if not _is_canonical_direct_name(name):
            raise SnapshotTransferReconcileIntegrityError(
                f"snapshots directory contains a non-canonical name: {path}"
            )
        if stat.S_ISLNK(info.st_mode):
            raise SnapshotTransferReconcileIntegrityError(
                f"unsafe symlinked snapshot entry: {path}"
            )
        if name == snapshot_id:
            selected_snapshot = info
        if name == staging_name:
            selected_staging = info
        if is_staging_snapshot_name(name):
            continue
        if is_published_snapshot_id(name) and stat.S_ISDIR(info.st_mode):
            published.append(name)
            published_identities[name] = _child_inode(info)
            if len(published) > MAX_PUBLISHED_SNAPSHOTS:
                raise SnapshotTransferReconcileError(
                    "published snapshot count exceeds bound "
                    f"{MAX_PUBLISHED_SNAPSHOTS}"
                )
            continue
        if name == snapshot_id:
            continue
        raise SnapshotTransferReconcileIntegrityError(
            f"unexpected unsafe snapshots entry is not published history: {path}"
        )
    return {
        "published": published,
        "published_identities": published_identities,
        "selected_snapshot": selected_snapshot,
        "selected_staging": selected_staging,
        "snapshots_inode": _child_inode(held),
    }


def _child_presence_token(
    info: Optional[os.stat_result], *, require_directory: bool, path: Path, label: str
) -> Optional[Tuple[int, int, int]]:
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotTransferReconcileIntegrityError(
            f"{label} must be a real directory, not a symlink: {path}"
        )
    if require_directory and not stat.S_ISDIR(info.st_mode):
        raise SnapshotTransferReconcileIntegrityError(
            f"{label} is not a real directory: {path}"
        )
    return (info.st_dev, info.st_ino, info.st_mode)


def _scan_graph(
    graph_path: Path,
    graph_fd: int,
    graph_identity: Tuple[int, int, int, int, int],
    snapshots_path: Path,
    snapshots_fd: int,
    snapshots_identity: Tuple[int, int, int, int, int],
    snapshot_id: str,
    staging_name: str,
    *,
    snapshot_label: str,
    staging_label: str,
    include_staging: bool,
) -> Dict[str, Any]:
    _require_held_directory_inode(
        graph_path,
        graph_fd,
        (graph_identity[0], graph_identity[1]),
        label="graph root",
    )
    _require_held_directory_inode(
        snapshots_path,
        snapshots_fd,
        (snapshots_identity[0], snapshots_identity[1]),
        label="snapshots directory",
    )
    lock_identity = _held_lock_identity(graph_path, graph_fd)
    current, current_identity = _read_held_current(graph_path, graph_fd)
    listing = _list_held_snapshots(
        snapshots_path,
        snapshots_fd,
        snapshots_identity,
        snapshot_id,
        staging_name,
    )
    if current not in set(listing["published"]):
        raise SnapshotTransferReconcileIntegrityError(
            "current is not a member of the published snapshot set"
        )
    snapshot_path = snapshots_path / snapshot_id
    snapshot_token = _child_presence_token(
        listing["selected_snapshot"],
        require_directory=True,
        path=snapshot_path,
        label=snapshot_label,
    )
    staging_token = None
    if include_staging:
        staging_path = snapshots_path / staging_name
        staging_token = _child_presence_token(
            listing["selected_staging"],
            require_directory=True,
            path=staging_path,
            label=staging_label,
        )
    return {
        "lock_identity": lock_identity,
        "current": current,
        "current_identity": current_identity,
        "published": list(listing["published"]),
        "published_identities": dict(listing["published_identities"]),
        "selected_snapshot": listing["selected_snapshot"],
        "selected_staging": listing["selected_staging"],
        "snapshot_token": snapshot_token,
        "staging_token": staging_token,
        "graph_inode": (graph_identity[0], graph_identity[1]),
        "snapshots_inode": listing["snapshots_inode"],
    }


def _comparable_scan(scan: Mapping[str, Any], *, include_staging: bool) -> Dict[str, Any]:
    comparable = {
        "lock_identity": scan["lock_identity"],
        "current": scan["current"],
        "current_identity": scan["current_identity"],
        "published": list(scan["published"]),
        "published_identities": dict(scan["published_identities"]),
        "snapshot_token": scan["snapshot_token"],
        "graph_inode": scan["graph_inode"],
        "snapshots_inode": scan["snapshots_inode"],
    }
    if include_staging:
        comparable["staging_token"] = scan["staging_token"]
    return comparable


def _listing_token(
    present: Mapping[str, os.stat_result],
) -> Dict[str, Tuple[int, int, int, int, int, int]]:
    return {name: _complete_file_identity(info) for name, info in present.items()}


def _reject_hardlink_anomalies(
    present: Mapping[str, os.stat_result], snap_dir: Path
) -> None:
    seen: Dict[Tuple[int, int], str] = {}
    for name, info in present.items():
        if info.st_nlink != 1:
            raise SnapshotTransferReconcileIntegrityError(
                f"hardlink anomaly in snapshot payload: {snap_dir / name}"
            )
        key = (info.st_dev, info.st_ino)
        previous = seen.get(key)
        if previous is not None:
            raise SnapshotTransferReconcileIntegrityError(
                "hardlink anomaly: "
                f"{previous} and {name} share an inode in {snap_dir}"
            )
        seen[key] = name


def _observe_present_snapshot(
    snapshot_path: Path,
    snapshot_fd: int,
    snapshot_identity: Tuple[int, int, int, int, int],
    plan_snapshot_id: str,
    *,
    label: str,
    first_observation_hook,
) -> Tuple[
    List[Dict[str, Any]],
    str,
    Dict[str, int],
    Dict[str, Tuple[int, int, int, int, int, int]],
    Dict[str, Tuple[int, int, int, int, int, int]],
    Dict[str, str],
]:
    opened_identity = _directory_identity(os.fstat(snapshot_fd))
    try:
        _observe_held_directory(
            snapshot_path,
            snapshot_fd,
            snapshot_identity,
            label=label,
        )
        first_present = _observe_listing(snapshot_path, snapshot_fd, opened_identity)
    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error
    _reject_hardlink_anomalies(first_present, snapshot_path)
    payload_fds: Dict[str, int] = {}
    identities: Dict[str, Tuple[int, int, int, int, int, int]] = {}
    revisions: Dict[str, str] = {}
    sizes: Dict[str, int] = {}
    try:
        for name in _byte_sort(list(first_present)):
            try:
                fd, identity, revision, size = _open_held_payload(
                    snapshot_fd,
                    name,
                    snapshot_path / name,
                    first_present[name],
                )
            except SnapshotImportPlanError as error:
                raise _wrap_plan_error(error) from error
            payload_fds[name] = fd
            identities[name] = identity
            revisions[name] = revision
            sizes[name] = size
        first_records = [
            {
                "path": name,
                "size_bytes": sizes[name],
                "content_revision": revisions[name],
            }
            for name in _byte_sort(list(first_present))
        ]
        first_observation_hook(snapshot_path, first_records)
        try:
            second_dir = _observe_held_directory(
                snapshot_path,
                snapshot_fd,
                snapshot_identity,
                label=label,
            )
            second_present = _observe_listing(
                snapshot_path, snapshot_fd, opened_identity
            )
        except SnapshotImportPlanError as error:
            raise _wrap_plan_error(error) from error
        _after_second_listed(snapshot_path, second_present)
        if (
            second_dir != snapshot_identity
            or _listing_token(first_present) != _listing_token(second_present)
            or set(second_present) != set(payload_fds)
        ):
            raise SnapshotTransferReconcileIntegrityError(
                f"{label} listing or payload set changed during reconciliation"
            )
        for name in _byte_sort(list(payload_fds)):
            try:
                _reobserve_held_payload(
                    snapshot_fd,
                    name,
                    snapshot_path / name,
                    payload_fds[name],
                    identities[name],
                    revisions[name],
                )
            except SnapshotImportPlanError as error:
                raise _wrap_plan_error(error) from error
        try:
            final_dir = _observe_held_directory(
                snapshot_path,
                snapshot_fd,
                snapshot_identity,
                label=label,
            )
            final_present = _observe_listing(
                snapshot_path, snapshot_fd, opened_identity
            )
        except SnapshotImportPlanError as error:
            raise _wrap_plan_error(error) from error
        if (
            final_dir != snapshot_identity
            or _listing_token(first_present) != _listing_token(final_present)
        ):
            raise SnapshotTransferReconcileIntegrityError(
                f"{label} listing, manifest, or payload changed "
                "during reconciliation"
            )
        try:
            if MANIFEST_NAME not in payload_fds:
                raise SnapshotTransferReconcileIntegrityError(
                    f"{label} is missing required payload manifest.json"
                )
            manifest_bytes = _read_held_bytes(
                payload_fds[MANIFEST_NAME],
                path=snapshot_path / MANIFEST_NAME,
                label="manifest",
                max_bytes=MAX_MANIFEST_BYTES,
            )
            manifest = _parse_manifest(manifest_bytes, snapshot_path / MANIFEST_NAME)
            observed_id = _validate_source_envelope(
                snapshot_path, payload_fds, first_present, manifest
            )
        except SnapshotImportPlanError as error:
            raise _wrap_plan_error(error) from error
        if observed_id != plan_snapshot_id:
            raise SnapshotTransferReconcileIntegrityError(
                f"{label} manifest id differs from the saved plan snapshot id"
            )
        observed_revision = export_revision_of(
            {
                "schema_version": PLAN_SCHEMA_VERSION,
                "resolved_snapshot": plan_snapshot_id,
                "files": first_records,
            }
        )
        return (
            first_records,
            observed_revision,
            payload_fds,
            _listing_token(first_present),
            identities,
            revisions,
        )
    except Exception:
        for fd in payload_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _reobserve_payloads(
    snapshot_path: Path,
    snapshot_fd: int,
    snapshot_identity: Tuple[int, int, int, int, int],
    expected_listing: Mapping[str, Tuple[int, int, int, int, int, int]],
    payload_fds: Mapping[str, int],
    identities: Mapping[str, Tuple[int, int, int, int, int, int]],
    revisions: Mapping[str, str],
    *,
    label: str,
) -> None:
    opened_identity = _directory_identity(os.fstat(snapshot_fd))
    try:
        before_dir = _observe_held_directory(
            snapshot_path,
            snapshot_fd,
            snapshot_identity,
            label=label,
        )
        before_present = _observe_listing(
            snapshot_path, snapshot_fd, opened_identity
        )
    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error
    if (
        before_dir != snapshot_identity
        or _listing_token(before_present) != dict(expected_listing)
        or set(before_present) != set(payload_fds)
        or set(payload_fds) != set(identities)
        or set(payload_fds) != set(revisions)
    ):
        raise SnapshotTransferReconcileIntegrityError(
            f"{label} listing or payload set changed before the final recheck"
        )
    for name in _byte_sort(list(payload_fds)):
        try:
            _reobserve_held_payload(
                snapshot_fd,
                name,
                snapshot_path / name,
                payload_fds[name],
                identities[name],
                revisions[name],
            )
        except SnapshotImportPlanError as error:
            raise _wrap_plan_error(error) from error
        _after_payload_final_recheck(snapshot_path, name)


def _require_stable_snapshot_listing(
    snapshot_path: Path,
    snapshot_fd: int,
    snapshot_identity: Tuple[int, int, int, int, int],
    expected_listing: Mapping[str, Tuple[int, int, int, int, int, int]],
    payload_fds: Mapping[str, int],
    *,
    label: str,
) -> None:
    opened_identity = _directory_identity(os.fstat(snapshot_fd))
    try:
        final_dir = _observe_held_directory(
            snapshot_path,
            snapshot_fd,
            snapshot_identity,
            label=label,
        )
        final_present = _observe_listing(
            snapshot_path, snapshot_fd, opened_identity
        )
    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error
    if (
        final_dir != snapshot_identity
        or _listing_token(final_present) != dict(expected_listing)
        or set(final_present) != set(payload_fds)
    ):
        raise SnapshotTransferReconcileIntegrityError(
            f"{label} listing or payload changed during the final recheck"
        )


def _classify_snapshot(
    observed_revision: Optional[str], planned_revision: str
) -> Tuple[str, bool]:
    if observed_revision is None:
        return _STATE_ABSENT, False
    if observed_revision == planned_revision:
        return _STATE_MATCHES, True
    return _STATE_MISMATCH, False


def _require_stable_presence(
    first_info: Optional[os.stat_result],
    final_info: Optional[os.stat_result],
    held_fd: Optional[int],
    path: Path,
    *,
    label: str,
) -> None:
    if (first_info is None) != (final_info is None):
        raise SnapshotTransferReconcileIntegrityError(
            f"{label} changed during reconciliation: {path}"
        )
    if first_info is None and final_info is None:
        return
    assert first_info is not None and final_info is not None
    if held_fd is None:
        raise SnapshotTransferReconcileIntegrityError(
            f"{label} changed during reconciliation: {path}"
        )
    held = os.fstat(held_fd)
    if (
        stat.S_ISLNK(final_info.st_mode)
        or not stat.S_ISDIR(final_info.st_mode)
        or _child_inode(final_info) != _child_inode(held)
        or _child_inode(first_info) != _child_inode(held)
    ):
        raise SnapshotTransferReconcileIntegrityError(
            f"{label} changed or was replaced: {path}"
        )


def _require_stable_staging(
    first_info: Optional[os.stat_result],
    final_info: Optional[os.stat_result],
    path: Path,
) -> None:
    if (first_info is None) != (final_info is None):
        raise SnapshotTransferReconcileIntegrityError(
            f"exact transfer staging changed during reconciliation: {path}"
        )
    if first_info is not None and final_info is not None:
        if _child_inode(first_info) != _child_inode(final_info):
            raise SnapshotTransferReconcileIntegrityError(
                f"exact transfer staging changed or was replaced: {path}"
            )


def _build_result(
    *,
    plan: Mapping[str, Any],
    plan_file: Path,
    apply_result_file: Optional[Path],
    apply_supplied: bool,
    apply_valid: bool,
    declared_outcome: str,
    canonical_source: Path,
    canonical_target: Path,
    source_current: str,
    source_published: Sequence[str],
    source_state: str,
    observed_source_revision: Optional[str],
    source_matches: bool,
    target_current: str,
    target_published: Sequence[str],
    target_state: str,
    observed_target_revision: Optional[str],
    target_matches: bool,
    staging_present: bool,
) -> Dict[str, Any]:
    snapshot_id = str(plan["snapshot_id"])
    planned = [dict(item) for item in plan["files"]]
    source_published_list = list(source_published)
    target_published_list = list(target_published)
    source_history_matches = source_published_list == list(
        plan["source_published_snapshots"]
    )
    expected_target_history = _byte_sort(
        list(plan["target_published_snapshots"]) + [snapshot_id]
    )
    if snapshot_id in set(plan["target_published_snapshots"]):
        expected_target_history = list(plan["target_published_snapshots"])
    target_history_matches = target_published_list == expected_target_history
    return {
        "schema_version": RECONCILE_SCHEMA_VERSION,
        "ok": True,
        "source_graph": str(canonical_source),
        "target_graph": str(canonical_target),
        "plan_file": str(plan_file),
        "apply_result_file": None if apply_result_file is None else str(apply_result_file),
        "input_transfer_revision": plan["transfer_revision"],
        "source_export_revision": plan["source_export_revision"],
        "snapshot_id": snapshot_id,
        "planned_files": planned,
        "file_count": plan["file_count"],
        "total_size_bytes": plan["total_size_bytes"],
        "apply_result_supplied": apply_supplied,
        "apply_result_valid": apply_valid,
        "declared_apply_outcome": declared_outcome,
        "source_current": source_current,
        "source_current_matches_plan": source_current == plan["source_current"],
        "source_published_snapshots": source_published_list,
        "source_published_count": len(source_published_list),
        "source_history_matches_plan": source_history_matches,
        "source_snapshot_present": source_state != _STATE_ABSENT,
        "source_snapshot_state": source_state,
        "observed_source_export_revision": observed_source_revision,
        "source_snapshot_matches_plan": source_matches,
        "target_current": target_current,
        "target_current_matches_plan": target_current == plan["target_current"],
        "target_published_snapshots": target_published_list,
        "target_published_count": len(target_published_list),
        "target_history_matches_plan_plus_snapshot": target_history_matches,
        "target_snapshot_present": target_state != _STATE_ABSENT,
        "target_snapshot_state": target_state,
        "observed_target_export_revision": observed_target_revision,
        "target_snapshot_matches_plan": target_matches,
        "target_staging_name": plan["target_staging_name"],
        "target_staging_present": staging_present,
        "transfer_cause_proven": False,
        "staging_cause_proven": False,
        "recovery_performed": False,
        "source_graph_mutated": False,
        "target_graph_mutated": False,
        "activation_performed": False,
        "retention_performed": False,
        "fresh_plan_required_before_transfer": True,
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
def _ordered_shared_graph_leases(first: Path, second: Path) -> Iterator[None]:
    """Hold two shared existing-lock leases in the documented global order."""
    with graph_shared_leases(first, second):
        yield


def _observe_optional_snapshot(
    snapshots_fd: int,
    snapshot_id: str,
    snapshot_path: Path,
    first_info: Optional[os.stat_result],
    planned_revision: str,
    *,
    label: str,
    opened_hook,
    first_observation_hook,
) -> Tuple[
    Optional[int],
    Optional[Tuple[int, int, int, int, int]],
    Optional[str],
    str,
    bool,
    Dict[str, int],
    Dict[str, Tuple[int, int, int, int, int, int]],
    Dict[str, Tuple[int, int, int, int, int, int]],
    Dict[str, str],
]:
    if first_info is None:
        return None, None, None, _STATE_ABSENT, False, {}, {}, {}, {}
    snapshot_fd, snapshot_identity = _open_child_directory(
        snapshots_fd,
        snapshot_id,
        snapshot_path,
        first_info,
        label=label,
    )
    try:
        opened_hook(snapshot_path, snapshot_fd, snapshot_identity)
        (
            _records,
            observed_revision,
            payload_fds,
            expected_listing,
            payload_identities,
            payload_revisions,
        ) = _observe_present_snapshot(
            snapshot_path,
            snapshot_fd,
            snapshot_identity,
            snapshot_id,
            label=label,
            first_observation_hook=first_observation_hook,
        )
    except Exception:
        _close_fd(snapshot_fd)
        raise
    state, matches = _classify_snapshot(observed_revision, planned_revision)
    return (
        snapshot_fd,
        snapshot_identity,
        observed_revision,
        state,
        matches,
        payload_fds,
        expected_listing,
        payload_identities,
        payload_revisions,
    )


@contextmanager
def _snapshot_transfer_reconcile_scope(
    source_graph: Path,
    target_graph: Path,
    plan_file: Path,
    apply_result_file: Optional[Path] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield one reconcile result while both leases and descriptors stay held."""
    plan_path, raw_plan = _load_json_object(plan_file, label="plan file")
    plan = _validate_saved_plan(raw_plan)
    apply_supplied = apply_result_file is not None
    apply_valid = False
    declared_outcome = _DECLARED_NOT_SUPPLIED
    apply_path: Optional[Path] = None
    if apply_supplied:
        apply_path, raw_apply = _load_json_object(
            apply_result_file, label="apply-result file"
        )
        _outcome, _saved_apply = _validate_saved_apply_result(raw_apply, plan)
        apply_valid = True
        declared_outcome = _outcome
    try:
        _require_descriptor_reads()
    except SnapshotExportPlanError as error:
        raise SnapshotTransferReconcileError(str(error)) from error
    source_path, source_path_identity = _resolve_existing_real_directory(
        source_graph, label="source graph root"
    )
    _after_source_graph_path_inspected(source_path)
    target_path, target_path_identity = _resolve_existing_real_directory(
        target_graph, label="target graph root"
    )
    _after_target_graph_path_inspected(target_path)
    _reject_same_graph(
        source_path, source_path_identity, target_path, target_path_identity
    )
    _require_managed_graph(source_path)
    _require_managed_graph(target_path)
    _require_existing_regular_lock(source_path)
    _require_existing_regular_lock(target_path)
    _after_graphs_identified(source_path, target_path)
    source_fd: Optional[int] = None
    target_fd: Optional[int] = None
    source_snapshots_fd: Optional[int] = None
    target_snapshots_fd: Optional[int] = None
    source_snapshot_fd: Optional[int] = None
    target_snapshot_fd: Optional[int] = None
    source_payload_fds: Dict[str, int] = {}
    target_payload_fds: Dict[str, int] = {}
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
        target_path, target_fd, target_identity, _target_opened = (
            _open_anchored_directory(
                target_path, target_path_identity, label="target graph root"
            )
        )
        _reject_same_graph(
            source_path, source_identity, target_path, target_identity
        )
        first_root, second_root = ordered_graph_lease_pair(
            source_path,
            _inode(source_identity),
            target_path,
            _inode(target_identity),
        )
        with _ordered_shared_graph_leases(first_root, second_root):
            if plan["source_graph"] != str(source_path):
                raise SnapshotTransferReconcileIntegrityError(
                    f"saved plan source_graph {plan['source_graph']!r} does not "
                    f"match requested source graph {str(source_path)!r}"
                )
            if plan["target_graph"] != str(target_path):
                raise SnapshotTransferReconcileIntegrityError(
                    f"saved plan target_graph {plan['target_graph']!r} does not "
                    f"match requested target graph {str(target_path)!r}"
                )
            source_snapshots_path, source_snapshots_fd, source_snapshots_identity = (
                _open_anchored_snapshots(source_path, source_fd, source_identity)
            )
            target_snapshots_path, target_snapshots_fd, target_snapshots_identity = (
                _open_anchored_snapshots(target_path, target_fd, target_identity)
            )
            snapshot_id = str(plan["snapshot_id"])
            staging_name = str(plan["target_staging_name"])
            planned_revision = str(plan["source_export_revision"])
            source_snapshot_path = source_snapshots_path / snapshot_id
            target_snapshot_path = target_snapshots_path / snapshot_id
            staging_path = target_snapshots_path / staging_name
            first_source = _scan_graph(
                source_path,
                source_fd,
                source_identity,
                source_snapshots_path,
                source_snapshots_fd,
                source_snapshots_identity,
                snapshot_id,
                staging_name,
                snapshot_label="source snapshot",
                staging_label="source staging directory",
                include_staging=False,
            )
            first_target = _scan_graph(
                target_path,
                target_fd,
                target_identity,
                target_snapshots_path,
                target_snapshots_fd,
                target_snapshots_identity,
                snapshot_id,
                staging_name,
                snapshot_label="target snapshot",
                staging_label="transfer staging directory",
                include_staging=True,
            )
            _after_first_joint_scan(
                source_path, target_path, first_source, first_target
            )
            first_source_snapshot = _stat_child(
                source_snapshots_fd,
                snapshot_id,
                source_snapshot_path,
                label="source snapshot",
            )
            _after_source_snapshot_first_stat(
                source_snapshot_path, first_source_snapshot
            )
            first_target_snapshot = _stat_child(
                target_snapshots_fd,
                snapshot_id,
                target_snapshot_path,
                label="target snapshot",
            )
            _after_target_snapshot_first_stat(
                target_snapshot_path, first_target_snapshot
            )
            first_staging = _stat_child(
                target_snapshots_fd,
                staging_name,
                staging_path,
                label="transfer staging directory",
            )
            _after_target_staging_first_stat(staging_path, first_staging)
            source_snapshot_identity: Optional[Tuple[int, int, int, int, int]] = None
            target_snapshot_identity: Optional[Tuple[int, int, int, int, int]] = None
            source_expected_listing: Dict[
                str, Tuple[int, int, int, int, int, int]
            ] = {}
            target_expected_listing: Dict[
                str, Tuple[int, int, int, int, int, int]
            ] = {}
            source_payload_identities: Dict[
                str, Tuple[int, int, int, int, int, int]
            ] = {}
            target_payload_identities: Dict[
                str, Tuple[int, int, int, int, int, int]
            ] = {}
            source_payload_revisions: Dict[str, str] = {}
            target_payload_revisions: Dict[str, str] = {}
            (
                source_snapshot_fd,
                source_snapshot_identity,
                observed_source_revision,
                source_state,
                source_matches,
                source_payload_fds,
                source_expected_listing,
                source_payload_identities,
                source_payload_revisions,
            ) = _observe_optional_snapshot(
                source_snapshots_fd,
                snapshot_id,
                source_snapshot_path,
                first_source_snapshot,
                planned_revision,
                label="source snapshot",
                opened_hook=_after_source_snapshot_opened,
                first_observation_hook=_after_first_source_snapshot_observation,
            )
            (
                target_snapshot_fd,
                target_snapshot_identity,
                observed_target_revision,
                target_state,
                target_matches,
                target_payload_fds,
                target_expected_listing,
                target_payload_identities,
                target_payload_revisions,
            ) = _observe_optional_snapshot(
                target_snapshots_fd,
                snapshot_id,
                target_snapshot_path,
                first_target_snapshot,
                planned_revision,
                label="target snapshot",
                opened_hook=_after_target_snapshot_opened,
                first_observation_hook=_after_first_target_snapshot_observation,
            )
            second_source = _scan_graph(
                source_path,
                source_fd,
                source_identity,
                source_snapshots_path,
                source_snapshots_fd,
                source_snapshots_identity,
                snapshot_id,
                staging_name,
                snapshot_label="source snapshot",
                staging_label="source staging directory",
                include_staging=False,
            )
            second_target = _scan_graph(
                target_path,
                target_fd,
                target_identity,
                target_snapshots_path,
                target_snapshots_fd,
                target_snapshots_identity,
                snapshot_id,
                staging_name,
                snapshot_label="target snapshot",
                staging_label="transfer staging directory",
                include_staging=True,
            )
            if _comparable_scan(first_source, include_staging=False) != _comparable_scan(
                second_source, include_staging=False
            ) or _comparable_scan(first_target, include_staging=True) != _comparable_scan(
                second_target, include_staging=True
            ):
                raise SnapshotTransferReconcileIntegrityError(
                    "publication lock, current, snapshots listing, selected "
                    "snapshot, or exact staging changed during reconciliation"
                )
            _after_second_joint_scan(
                source_path, target_path, second_source, second_target
            )
            if first_source_snapshot is not None:
                assert source_snapshot_fd is not None
                assert source_snapshot_identity is not None
                _reobserve_payloads(
                    source_snapshot_path,
                    source_snapshot_fd,
                    source_snapshot_identity,
                    source_expected_listing,
                    source_payload_fds,
                    source_payload_identities,
                    source_payload_revisions,
                    label="source snapshot",
                )
            if first_target_snapshot is not None:
                assert target_snapshot_fd is not None
                assert target_snapshot_identity is not None
                _reobserve_payloads(
                    target_snapshot_path,
                    target_snapshot_fd,
                    target_snapshot_identity,
                    target_expected_listing,
                    target_payload_fds,
                    target_payload_identities,
                    target_payload_revisions,
                    label="target snapshot",
                )
            if first_source_snapshot is not None:
                assert source_snapshot_fd is not None
                assert source_snapshot_identity is not None
                _require_stable_snapshot_listing(
                    source_snapshot_path,
                    source_snapshot_fd,
                    source_snapshot_identity,
                    source_expected_listing,
                    source_payload_fds,
                    label="source snapshot",
                )
            if first_target_snapshot is not None:
                assert target_snapshot_fd is not None
                assert target_snapshot_identity is not None
                _require_stable_snapshot_listing(
                    target_snapshot_path,
                    target_snapshot_fd,
                    target_snapshot_identity,
                    target_expected_listing,
                    target_payload_fds,
                    label="target snapshot",
                )
            third_source = _scan_graph(
                source_path,
                source_fd,
                source_identity,
                source_snapshots_path,
                source_snapshots_fd,
                source_snapshots_identity,
                snapshot_id,
                staging_name,
                snapshot_label="source snapshot",
                staging_label="source staging directory",
                include_staging=False,
            )
            third_target = _scan_graph(
                target_path,
                target_fd,
                target_identity,
                target_snapshots_path,
                target_snapshots_fd,
                target_snapshots_identity,
                snapshot_id,
                staging_name,
                snapshot_label="target snapshot",
                staging_label="transfer staging directory",
                include_staging=True,
            )
            if _comparable_scan(second_source, include_staging=False) != _comparable_scan(
                third_source, include_staging=False
            ) or _comparable_scan(
                second_target, include_staging=True
            ) != _comparable_scan(third_target, include_staging=True):
                raise SnapshotTransferReconcileIntegrityError(
                    "publication lock, current, snapshots listing, selected "
                    "snapshot, or exact staging changed during the final "
                    "payload recheck"
                )
            final_source_snapshot = _stat_child(
                source_snapshots_fd,
                snapshot_id,
                source_snapshot_path,
                label="source snapshot",
            )
            final_target_snapshot = _stat_child(
                target_snapshots_fd,
                snapshot_id,
                target_snapshot_path,
                label="target snapshot",
            )
            final_staging = _stat_child(
                target_snapshots_fd,
                staging_name,
                staging_path,
                label="transfer staging directory",
            )
            _require_stable_presence(
                first_source_snapshot,
                final_source_snapshot,
                source_snapshot_fd,
                source_snapshot_path,
                label="source snapshot",
            )
            _require_stable_presence(
                first_target_snapshot,
                final_target_snapshot,
                target_snapshot_fd,
                target_snapshot_path,
                label="target snapshot",
            )
            _require_stable_staging(first_staging, final_staging, staging_path)
            if first_source_snapshot is None:
                source_state = _STATE_ABSENT
                observed_source_revision = None
                source_matches = False
            if first_target_snapshot is None:
                target_state = _STATE_ABSENT
                observed_target_revision = None
                target_matches = False
            result = _build_result(
                plan=plan,
                plan_file=plan_path,
                apply_result_file=apply_path,
                apply_supplied=apply_supplied,
                apply_valid=apply_valid,
                declared_outcome=declared_outcome,
                canonical_source=source_path,
                canonical_target=target_path,
                source_current=str(third_source["current"]),
                source_published=list(third_source["published"]),
                source_state=source_state,
                observed_source_revision=observed_source_revision,
                source_matches=source_matches,
                target_current=str(third_target["current"]),
                target_published=list(third_target["published"]),
                target_state=target_state,
                observed_target_revision=observed_target_revision,
                target_matches=target_matches,
                staging_present=final_staging is not None,
            )
            _after_result_ready(
                source_path,
                target_path,
                source_fd,
                target_fd,
                source_snapshots_fd,
                target_snapshots_fd,
                source_snapshot_fd,
                target_snapshot_fd,
                source_payload_fds,
                target_payload_fds,
                result,
            )
            yield result
    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error
    except SnapshotStagingError as error:
        raise _wrap_plan_error(error) from error
    except SnapshotTransferPlanError as error:
        raise _wrap_plan_error(error) from error
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error
    finally:
        for fd in source_payload_fds.values():
            _close_fd(fd)
        for fd in target_payload_fds.values():
            _close_fd(fd)
        _close_fd(source_snapshot_fd)
        _close_fd(target_snapshot_fd)
        _close_fd(source_snapshots_fd)
        _close_fd(target_snapshots_fd)
        _close_fd(source_fd)
        _close_fd(target_fd)


def snapshot_transfer_reconcile(
    source_graph: Path,
    target_graph: Path,
    plan_file: Path,
    apply_result_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reconcile one saved transfer plan against both graphs without writing."""
    with _snapshot_transfer_reconcile_scope(
        source_graph, target_graph, plan_file, apply_result_file
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile a saved snapshot-transfer-plan and optional saved "
            "snapshot-transfer-apply result against two managed BYOG graphs. "
            "Observation only. Does not retry, recover, copy, publish, "
            "activate, pin, prune, clean staging, run retention, or mutate "
            "either graph. Never creates .publish.lock, and is not an MCP "
            "tool. A fresh transfer plan is required before any later apply."
        )
    )
    parser.add_argument(
        "--source-graph",
        type=Path,
        required=True,
        help="Managed source BYOG graph root, relative to cwd.",
    )
    parser.add_argument(
        "--target-graph",
        type=Path,
        required=True,
        help="Managed target BYOG graph root, relative to cwd.",
    )
    parser.add_argument(
        "--plan-file",
        type=Path,
        required=True,
        help="Saved schema-1 snapshot-transfer-plan JSON, relative to cwd.",
    )
    parser.add_argument(
        "--apply-result-file",
        type=Path,
        default=None,
        help="Optional saved schema-1 snapshot-transfer-apply JSON.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_transfer_reconcile_scope(
            args.source_graph,
            args.target_graph,
            args.plan_file,
            args.apply_result_file,
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            sys.stdout.flush()
            return 0 if result["ok"] else 1
    except SnapshotTransferReconcileError as error:
        print(f"snapshot-transfer-reconcile: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-transfer-reconcile: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
