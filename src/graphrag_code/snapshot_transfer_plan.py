#!/usr/bin/env python
"""Read-only snapshot transfer plan.

``snapshot-transfer-plan`` inspects one retained published snapshot in a
managed BYOG graph and one different managed BYOG graph, then emits a
deterministic schema-1 plan for a future direct transfer of that
snapshot. It does not create a standalone export directory, copy,
rename, link, replace, truncate, chmod, unlink, or delete anything,
change ``current``, or create or modify ``.publish.lock``,
``.snapshot-pins.json``, or any writer lock. It does not activate, pin,
prune, clean staging, export, import, restore, recover, or repair.
``transfer_performed`` is always false. ``source_graph_mutated`` and
``target_graph_mutated`` are always false.
``fresh_plan_required_before_transfer`` is always true.

This command proves only the language-independent stored snapshot
envelope and observed bytes. It does not compare ``source_root``,
``git_commit``, or ``created_at`` with the current host, and it does
not run any language-specific or Clang overlay audit. The plan is not
a backup and is not a claim of authenticity, provenance, portability,
recoverability, or successful future transfer. A later transfer apply
must freshly reproduce the complete plan. ``transfer_revision`` is a
self-consistency/CAS-ready token accepted only by
``snapshot-transfer-apply`` after that command freshly reproduces the
same plan.

Both graph arguments may be relative to the invoking cwd. Each must be
an existing real directory, never a symlink, and a managed
``current + snapshots/`` graph with an already-existing regular
``.publish.lock``. The command never creates, adopts, truncates,
chmods, or replaces either lock. Legacy-flat and unlocked managed
graphs fail closed.

The source and target must be different directory identities, including
two path aliases that resolve to the same ``(st_dev, st_ino)``. Same
graph identity is malformed invocation: exit 2 and empty stdout. The
identity check happens before any nested lease.

One shared existing-lock lease is held on each graph for the complete
joint observation, result construction, serialization, stdout write,
and flush. The two leases are acquired in one deterministic global
order independent of source/target role: sort by canonical UTF-8 path
bytes of the real graph root, then by ``(st_dev, st_ino)`` as a stable
identity tie-breaker. Opposing A→B and B→A operations therefore cannot
create a lock cycle. A future source-shared/target-exclusive transfer
apply must use this same order.

Both graph roots and both ``snapshots/`` directories are anchored with
no-follow directory descriptors and identity checks. The command does
not invoke public export or import CLIs and does not create a detached
observation window.

MCP stays exactly 11 read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-transfer-plan --source-graph <managed-root> --snapshot <id|current> --target-graph <managed-root> [--json]
    python -m graphrag_code.snapshot_transfer_plan --source-graph <managed-root> --snapshot <id|current> --target-graph <managed-root> [--json]
    uv run python scripts/snapshot_transfer_plan.py --source-graph <managed-root> --snapshot <id|current> --target-graph <managed-root> [--json]
"""
from __future__ import annotations

import argparse
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
    STAGING_NAME_PREFIX,
    ByogPublicationLockError,
    ByogReaderLockError,
    _validate_managed_snapshot_layout,
    graph_lease_order_key,
    graph_read_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
    ordered_graph_lease_pair,
)
from graphrag_code.byog_snapshot_integrity import MANIFEST_NAME
from graphrag_code.snapshot_export_plan import (
    HASH_CHUNK_BYTES,
    MAX_MANIFEST_BYTES,
    PLAN_SCHEMA_VERSION,
    SnapshotExportPlanError,
    SnapshotExportPlanIntegrityError,
    _is_canonical_direct_name,
    _list_snapshots_entries,
    _open_directory_nofollow,
    _parse_snapshot,
    _require_descriptor_reads,
    export_revision_of,
)
from graphrag_code.snapshot_import_plan import (
    SnapshotImportPlanError,
    SnapshotImportPlanIntegrityError,
    _complete_directory_identity,
    _complete_file_identity,
    _listing_token,
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
    SnapshotStagingError,
    SnapshotStagingIntegrityError,
    _lock_identity,
    _read_current,
    build_stable_staging_inventory_unlocked,
)


PLAN_SCHEMA_VERSION_TRANSFER = 1
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BLOCK_PUBLISHED = "snapshot_id_already_published"
_BLOCK_STAGING = "target_staging_name_present"
_ALLOWED_BLOCKING = (_BLOCK_PUBLISHED, _BLOCK_STAGING)
_TRANSFER_REVISION_KEYS = (
    "schema_version",
    "snapshot_id",
    "source_export_revision",
    "target_current",
    "target_published_snapshots",
    "target_staging_name",
    "target_snapshot_present",
    "target_staging_present",
    "blocking_reasons",
    "transfer_ready",
    "source_envelope_valid",
    "transfer_performed",
    "fresh_plan_required_before_transfer",
)
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "plan_is_not_transfer",
        "kind": "notice",
        "message": (
            "snapshot-transfer-plan is inspection only. transfer_performed is "
            "always false. It does not create an export directory, copy "
            "files, or mutate either graph."
        ),
    },
    {
        "code": "plan_is_not_backup",
        "kind": "notice",
        "message": (
            "This plan is not a backup, archive, restore kit, or "
            "authorization to delete or transfer anything. It does not claim "
            "portability, recoverability, authenticity, or provenance."
        ),
    },
    {
        "code": "transfer_revision_is_self_consistency_only",
        "kind": "notice",
        "message": (
            "transfer_revision is a self-consistency/CAS-ready plan token for "
            "this exact observed source snapshot and target graph. No "
            "mutation command in this milestone accepts it. A future "
            "transfer apply must freshly reproduce the complete matching plan."
        ),
    },
    {
        "code": "fresh_plan_required_before_transfer",
        "kind": "notice",
        "message": (
            "fresh_plan_required_before_transfer is always true. A ready "
            "plan does not authorize a later apply without freshly "
            "reproducing the complete plan and matching transfer_revision."
        ),
    },
    {
        "code": "source_envelope_language_independent_only",
        "kind": "notice",
        "message": (
            "This milestone proves only the language-independent stored "
            "snapshot envelope and observed bytes. It does not compare "
            "source_root, git_commit, or created_at with the current host, "
            "and it does not run any language-specific or Clang overlay audit."
        ),
    },
    {
        "code": "advisory_locks_cooperating_only",
        "kind": "notice",
        "message": (
            "Advisory locks protect only cooperating processes. "
            "Multiple complete payload hash observations, including a final "
            "source recheck after target observation, plus identity and listing "
            "rechecks detect differences visible across those reads. This is "
            "not continuous protection against lock-ignoring actors, and "
            "changes after the final observation are not covered."
        ),
    },
    {
        "code": "cli_only_not_mcp",
        "kind": "notice",
        "message": (
            "snapshot-transfer-plan is CLI-only and intentionally absent "
            "from the fixed 11-tool MCP set."
        ),
    },
)
_UNRELATED_STAGING_NOTICE = {
    "code": "unrelated_target_staging_present",
    "kind": "notice",
    "message": (
        "Other staging entries are reported through target_staging_count and "
        "do not independently block this source snapshot id."
    ),
}


class SnapshotTransferPlanError(Exception):
    """Expected transfer-plan failure. Default exit 2."""

    exit_code = 2


class SnapshotTransferPlanIntegrityError(SnapshotTransferPlanError):
    """Unsafe structure, invalid envelope, or concurrent change. Exit 1."""

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
    ready = "true" if result.get("transfer_ready") is True else "false"
    return (
        "snapshot-transfer-plan: "
        f"source_graph={result.get('source_graph')} "
        f"target_graph={result.get('target_graph')} "
        f"requested={result.get('requested_snapshot')} "
        f"snapshot_id={result.get('snapshot_id')} "
        f"transfer_ready={ready} "
        f"transfer_revision={result.get('transfer_revision')} "
        "transfer_performed=false "
        "fresh_plan_required_before_transfer=true "
        "This plan is not a transfer and is not a backup."
    )


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def _inode(identity: Tuple[int, ...]) -> Tuple[int, int]:
    return (int(identity[0]), int(identity[1]))


def _from_export_error(error: Exception) -> SnapshotTransferPlanError:
    message = str(error)
    if isinstance(error, SnapshotExportPlanIntegrityError):
        return SnapshotTransferPlanIntegrityError(message)
    if isinstance(error, SnapshotExportPlanError):
        lowered = message.lower()
        if (
            "exceeds bound" in lowered
            or "unsupported" in lowered
        ):
            wrapped = SnapshotTransferPlanError(message)
            wrapped.exit_code = getattr(error, "exit_code", 2)
            return wrapped
        return SnapshotTransferPlanIntegrityError(message)
    return SnapshotTransferPlanError(message)


def _from_import_error(error: Exception) -> SnapshotTransferPlanError:
    message = str(error)
    if isinstance(error, SnapshotImportPlanIntegrityError):
        return SnapshotTransferPlanIntegrityError(message)
    if isinstance(error, SnapshotImportPlanError):
        lowered = message.lower()
        if (
            "exceeds bound" in lowered
            or "unsupported" in lowered
            or "is required to read parquet" in lowered
        ):
            wrapped = SnapshotTransferPlanError(message)
            wrapped.exit_code = getattr(error, "exit_code", 2)
            return wrapped
        return SnapshotTransferPlanIntegrityError(message)
    return SnapshotTransferPlanError(message)


def _wrap_staging_error(error: Exception) -> SnapshotTransferPlanError:
    if isinstance(error, SnapshotStagingIntegrityError):
        return SnapshotTransferPlanIntegrityError(str(error))
    if isinstance(error, SnapshotStagingError):
        message = str(error)
        lowered = message.lower()
        if "exceeds bound" in lowered or "unsupported" in lowered:
            wrapped = SnapshotTransferPlanError(message)
            wrapped.exit_code = getattr(error, "exit_code", 2)
            return wrapped
        return SnapshotTransferPlanIntegrityError(message)
    return SnapshotTransferPlanError(str(error))


def _lock_error(error: Exception) -> SnapshotTransferPlanError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotTransferPlanError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotTransferPlanIntegrityError(message)
    return SnapshotTransferPlanError(message)


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotTransferPlanError(str(error)) from error
    if not managed:
        raise SnapshotTransferPlanError(
            "legacy flat-parquet directory has no retained snapshot history: "
            f"{root}"
        )


def _resolve_existing_real_directory(
    path: Path, *, label: str
) -> Tuple[Path, Tuple[int, int, int, int, int]]:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    try:
        before = resolved.lstat()
    except FileNotFoundError as error:
        raise SnapshotTransferPlanError(f"{label} does not exist: {resolved}") from error
    except OSError as error:
        raise SnapshotTransferPlanError(
            f"cannot inspect {label} {resolved}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotTransferPlanError(
            f"{label} must be a real directory, not a symlink: {resolved}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotTransferPlanError(f"{label} is not a real directory: {resolved}")
    return resolved, _complete_directory_identity(before)


def _require_path_identity(
    path: Path,
    expected_identity: Tuple[int, int, int, int, int],
    *,
    label: str,
) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise SnapshotTransferPlanIntegrityError(
            f"{label} changed before it was anchored: {path}"
        ) from error
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _complete_directory_identity(current) != expected_identity
    ):
        raise SnapshotTransferPlanIntegrityError(
            f"{label} changed or was replaced before it was anchored: {path}"
        )


def _observe_directory(
    path: Path,
    directory_fd: int,
    expected_identity: Tuple[int, int, int, int, int],
    *,
    label: str,
) -> Tuple[int, int, int, int, int]:
    try:
        return _observe_held_directory(
            path, directory_fd, expected_identity, label=label
        )
    except SnapshotImportPlanError as error:
        raise _from_import_error(error) from error


def _reject_same_graph(
    source: Path,
    source_identity: Tuple[int, ...],
    target: Path,
    target_identity: Tuple[int, ...],
) -> None:
    if _inode(source_identity) == _inode(target_identity):
        raise SnapshotTransferPlanError(
            "source-graph and target-graph must be different directory "
            f"identities: {source} and {target}"
        )


def _open_anchored_directory(
    path: Path,
    expected_path_identity: Tuple[int, int, int, int, int],
    *,
    label: str,
) -> Tuple[Path, int, Tuple[int, int, int, int, int], Tuple[int, int, int, int]]:
    try:
        directory_fd, opened_identity = _open_directory_nofollow(path, label=label)
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _from_export_error(error) from error
    try:
        opened = os.fstat(directory_fd)
        identity = _complete_directory_identity(opened)
        if identity != expected_path_identity:
            raise SnapshotTransferPlanIntegrityError(
                f"{label} changed before it was anchored: {path}"
            )
        _observe_directory(path, directory_fd, identity, label=label)
        try:
            canonical = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SnapshotTransferPlanIntegrityError(
                f"{label} changed during canonicalization: {path}"
            ) from error
        _observe_directory(canonical, directory_fd, identity, label=label)
        if opened_identity != (
            opened.st_dev,
            opened.st_ino,
            opened.st_mtime_ns,
            opened.st_mode,
        ):
            raise SnapshotTransferPlanIntegrityError(
                f"{label} changed before it was anchored: {path}"
            )
        return canonical, directory_fd, identity, opened_identity
    except Exception:
        os.close(directory_fd)
        raise


def _open_snapshots_directory(
    root: Path,
    root_fd: int,
    root_identity: Tuple[int, int, int, int, int],
) -> Tuple[Path, int, Tuple[int, int, int, int, int], Tuple[int, int, int, int]]:
    _observe_directory(root, root_fd, root_identity, label="graph root")
    snapshots = root / "snapshots"
    try:
        before = snapshots.lstat()
    except FileNotFoundError as error:
        raise SnapshotTransferPlanError(
            f"snapshots directory does not exist: {snapshots}"
        ) from error
    except OSError as error:
        raise SnapshotTransferPlanError(
            f"cannot inspect snapshots directory {snapshots}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotTransferPlanIntegrityError(
            f"snapshots directory must be a real directory, not a symlink: {snapshots}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotTransferPlanIntegrityError(
            f"snapshots directory is not a real directory: {snapshots}"
        )
    expected = _complete_directory_identity(before)
    return _open_anchored_directory(
        snapshots, expected, label="snapshots directory"
    )


def canonical_transfer_revision_payload(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Decision inputs bound by ``transfer_revision``.

    Compact UTF-8 JSON with sorted keys, no trailing newline binds the
    listed decision fields. The complete ordered source file records
    are bound indirectly by ``source_export_revision``. Absolute graph
    paths, counts, ``requested_snapshot``, notices, ``ok``, and the
    mutation presentation flags are not revision inputs.
    """
    payload: Dict[str, Any] = {}
    for key in _TRANSFER_REVISION_KEYS:
        if key not in result:
            raise SnapshotTransferPlanError(
                f"transfer plan is missing revision input {key!r}"
            )
        payload[key] = result[key]

    schema = payload["schema_version"]
    if isinstance(schema, bool) or schema != PLAN_SCHEMA_VERSION_TRANSFER:
        raise SnapshotTransferPlanError("transfer plan schema_version must be 1")

    snapshot_id = payload["snapshot_id"]
    if not isinstance(snapshot_id, str) or not is_published_snapshot_id(snapshot_id):
        raise SnapshotTransferPlanError(
            "transfer plan snapshot_id is not a canonical published id"
        )

    source_revision = payload["source_export_revision"]
    if not isinstance(source_revision, str) or not _REVISION_RE.fullmatch(
        source_revision
    ):
        raise SnapshotTransferPlanError(
            "transfer plan source_export_revision must be sha256:<64 lowercase hex>"
        )

    current = payload["target_current"]
    if not isinstance(current, str) or not is_published_snapshot_id(current):
        raise SnapshotTransferPlanError(
            "transfer plan target_current is not a canonical published id"
        )

    published = payload["target_published_snapshots"]
    if not isinstance(published, list) or any(
        not isinstance(item, str) for item in published
    ):
        raise SnapshotTransferPlanError(
            "transfer plan target_published_snapshots must be a list of snapshot ids"
        )
    if any(not is_published_snapshot_id(item) for item in published):
        raise SnapshotTransferPlanError(
            "transfer plan target_published_snapshots contains a non-canonical id"
        )
    if len(set(published)) != len(published) or list(published) != _byte_sort(
        published
    ):
        raise SnapshotTransferPlanError(
            "transfer plan target_published_snapshots must be unique and "
            "sorted in UTF-8-byte order"
        )
    if current not in published:
        raise SnapshotTransferPlanError(
            "transfer plan target_current is not a member of target_published_snapshots"
        )

    staging_name = payload["target_staging_name"]
    expected_staging = f"{STAGING_NAME_PREFIX}{snapshot_id}"
    if (
        not isinstance(staging_name, str)
        or not _is_canonical_direct_name(staging_name)
        or staging_name != expected_staging
    ):
        raise SnapshotTransferPlanError(
            "transfer plan target_staging_name must be exactly .staging-<snapshot-id>"
        )

    for flag in (
        "target_snapshot_present",
        "target_staging_present",
        "transfer_ready",
        "source_envelope_valid",
        "transfer_performed",
        "fresh_plan_required_before_transfer",
    ):
        value = payload[flag]
        if type(value) is not bool:
            raise SnapshotTransferPlanError(
                f"transfer plan {flag} must be a JSON boolean, not {value!r}"
            )

    reasons = payload["blocking_reasons"]
    if not isinstance(reasons, list) or any(
        not isinstance(item, str) for item in reasons
    ):
        raise SnapshotTransferPlanError(
            "transfer plan blocking_reasons must be a list of strings"
        )
    if any(item not in _ALLOWED_BLOCKING for item in reasons):
        raise SnapshotTransferPlanError(
            "transfer plan blocking_reasons contains an unsupported code"
        )
    if len(set(reasons)) != len(reasons) or list(reasons) != _byte_sort(reasons):
        raise SnapshotTransferPlanError(
            "transfer plan blocking_reasons must be unique and sorted in UTF-8-byte order"
        )
    if payload["source_envelope_valid"] is not True:
        raise SnapshotTransferPlanError(
            "transfer plan source_envelope_valid must be true"
        )
    if payload["transfer_performed"] is not False:
        raise SnapshotTransferPlanError(
            "transfer plan transfer_performed must be false"
        )
    if payload["fresh_plan_required_before_transfer"] is not True:
        raise SnapshotTransferPlanError(
            "transfer plan fresh_plan_required_before_transfer must be true"
        )
    expected_snapshot_present = snapshot_id in set(published)
    if payload["target_snapshot_present"] is not expected_snapshot_present:
        raise SnapshotTransferPlanError(
            "transfer plan target_snapshot_present does not match "
            "target_published_snapshots"
        )
    expected_reasons: List[str] = []
    if expected_snapshot_present:
        expected_reasons.append(_BLOCK_PUBLISHED)
    if payload["target_staging_present"] is True:
        expected_reasons.append(_BLOCK_STAGING)
    expected_reasons = _byte_sort(expected_reasons)
    if list(reasons) != expected_reasons:
        raise SnapshotTransferPlanError(
            "transfer plan blocking_reasons do not match the target presence flags"
        )
    expected_ready = not expected_reasons
    if payload["transfer_ready"] is not expected_ready:
        raise SnapshotTransferPlanError(
            "transfer plan transfer_ready does not match its blocking reasons"
        )
    return payload


def canonical_transfer_revision_text(result: Mapping[str, Any]) -> str:
    return json.dumps(
        canonical_transfer_revision_payload(result),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def transfer_revision_of(result: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        canonical_transfer_revision_text(result).encode("utf-8")
    ).hexdigest()
    return "sha256:" + digest


def _after_transfer_source_path_inspected(_source: Path) -> None:
    """Test hook after initial source-path validation and before identity compare."""
    return None


def _after_transfer_target_path_inspected(_target: Path) -> None:
    """Test hook after initial target-path validation and before identity compare."""
    return None


def _after_transfer_graphs_identified(
    _source: Path, _target: Path
) -> None:
    """Test hook after same-graph rejection and before nested leases."""
    return None


def _after_transfer_source_tokens_captured(
    _root: Path, _tokens: Mapping[str, Any]
) -> None:
    """Test hook after the first source lock/current/listing capture."""
    return None


def _after_transfer_source_listed(
    _root: Path, _entries: Mapping[str, os.stat_result]
) -> None:
    """Test hook after the first anchored source payload listing."""
    return None


def _after_transfer_source_first_observation(
    _root: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after the first complete source payload observation."""
    return None


def _after_transfer_target_tokens_captured(
    _root: Path, _tokens: Mapping[str, Any]
) -> None:
    """Test hook after the first target token capture."""
    return None


def _after_transfer_before_source_final_recheck(
    _source: Path, _target: Path
) -> None:
    """Test hook after target observation and before the final source recheck."""
    return None


def _after_transfer_source_final_payload_recheck(
    _source: Path, _name: str
) -> None:
    """Test hook after one payload in the final source hash pass."""
    return None


def _after_transfer_source_final_recheck(
    _source: Path, _target: Path
) -> None:
    """Test hook after the final source recheck and before the target recheck."""
    return None


def _after_transfer_result_ready(
    _source: Path,
    _target: Path,
    _source_fd: int,
    _target_fd: int,
    _source_snapshots_fd: int,
    _target_snapshots_fd: int,
    _selected_fd: int,
    _payload_fds: Mapping[str, int],
    _result: Mapping[str, Any],
) -> None:
    """Test hook after the result is built and before it is yielded."""
    return None


def _capture_source_tokens(
    root: Path, *, requested: str, resolved: Optional[str]
) -> Dict[str, Any]:
    try:
        lock_identity = _lock_identity(root)
        current_value, current_identity = _read_current(root)
        listing = _list_snapshots_entries(root)
    except SnapshotStagingError as error:
        raise _wrap_staging_error(error) from error
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _from_export_error(error) from error
    published = [
        str(item["name"]) for item in listing if item.get("kind") == "published"
    ]
    selected_identity: Optional[Tuple[int, int, int, int, int]] = None
    if resolved is not None:
        selected_identity = _selected_directory_identity(root, resolved)
    return {
        "lock_identity": lock_identity,
        "current_value": current_value,
        "current_identity": current_identity,
        "listing": listing,
        "published": published,
        "selected_identity": selected_identity,
        "requested": requested,
    }


def _selected_directory_identity(
    root: Path, snapshot_id: str
) -> Tuple[int, int, int, int, int]:
    path = root / "snapshots" / snapshot_id
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotTransferPlanError(
            f"snapshot is not a retained published directory: {snapshot_id!r}"
        ) from error
    except OSError as error:
        raise SnapshotTransferPlanError(
            f"cannot inspect selected snapshot directory {path}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotTransferPlanIntegrityError(
            f"selected snapshot must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotTransferPlanIntegrityError(
            f"selected snapshot is not a real directory: {path}"
        )
    return _complete_directory_identity(info)


def _reject_hardlink_anomalies(
    present: Mapping[str, os.stat_result], snap_dir: Path
) -> None:
    seen: Dict[Tuple[int, int], str] = {}
    for name, info in present.items():
        if info.st_nlink != 1:
            raise SnapshotTransferPlanIntegrityError(
                f"hardlink anomaly in snapshot payload: {snap_dir / name}"
            )
        key = (info.st_dev, info.st_ino)
        previous = seen.get(key)
        if previous is not None:
            raise SnapshotTransferPlanIntegrityError(
                "hardlink anomaly: "
                f"{previous} and {name} share an inode in {snap_dir}"
            )
        seen[key] = name


def _capture_target_tokens(root: Path) -> Dict[str, Any]:
    try:
        consistency, inventory = build_stable_staging_inventory_unlocked(root)
    except SnapshotStagingError as error:
        raise _wrap_staging_error(error) from error
    return {
        "consistency": consistency,
        "inventory": inventory,
    }


def _observe_target_unlocked(root: Path, snapshot_id: str) -> Dict[str, Any]:
    _require_managed_graph(root)
    first = _capture_target_tokens(root)
    _after_transfer_target_tokens_captured(root, first)
    second = _capture_target_tokens(root)
    if first != second:
        raise SnapshotTransferPlanIntegrityError(
            "publication lock, current, snapshots listing, or staging changed "
            "during transfer plan"
        )
    inventory = first["inventory"]
    current = inventory.get("current")
    if not isinstance(current, str) or not is_published_snapshot_id(current):
        raise SnapshotTransferPlanIntegrityError(
            f"current is not a canonical published snapshot id: {current!r}"
        )
    published = list(inventory.get("published_snapshots") or [])
    if current not in published:
        raise SnapshotTransferPlanIntegrityError(
            "current is not a member of the published snapshot set"
        )
    staging_names = [
        str(item.get("name") or "")
        for item in (inventory.get("staging_entries") or [])
    ]
    target_staging_name = f"{STAGING_NAME_PREFIX}{snapshot_id}"
    target_snapshot_present = snapshot_id in set(published)
    target_staging_present = target_staging_name in set(staging_names)
    blocking: List[str] = []
    if target_snapshot_present:
        blocking.append(_BLOCK_PUBLISHED)
    if target_staging_present:
        blocking.append(_BLOCK_STAGING)
    blocking = _byte_sort(blocking)
    unrelated_staging = [name for name in staging_names if name != target_staging_name]
    return {
        "current": current,
        "published_snapshots": published,
        "target_staging_name": target_staging_name,
        "target_snapshot_present": target_snapshot_present,
        "target_staging_present": target_staging_present,
        "staging_count": len(staging_names),
        "blocking_reasons": blocking,
        "transfer_ready": not blocking,
        "unrelated_staging": unrelated_staging,
        "tokens": first,
    }


def _open_held_source_payload(
    directory_fd: int,
    name: str,
    path: Path,
    listed: os.stat_result,
) -> Tuple[int, Tuple[int, int, int, int, int, int], str, int]:
    try:
        return _open_held_payload(directory_fd, name, path, listed)
    except SnapshotImportPlanError as error:
        raise _from_import_error(error) from error
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _from_export_error(error) from error


def _reobserve_payload(
    directory_fd: int,
    name: str,
    path: Path,
    fd: int,
    expected_identity: Tuple[int, int, int, int, int, int],
    expected_revision: str,
) -> None:
    try:
        held = os.fstat(fd)
    except OSError as error:
        raise SnapshotTransferPlanIntegrityError(
            f"cannot inspect held payload descriptor {path}: {error}"
        ) from error
    if not stat.S_ISREG(held.st_mode) or held.st_nlink != 1:
        raise SnapshotTransferPlanIntegrityError(
            f"hardlink anomaly or non-regular held payload: {path}"
        )
    try:
        _reobserve_held_payload(
            directory_fd, name, path, fd, expected_identity, expected_revision
        )
    except SnapshotImportPlanError as error:
        raise _from_import_error(error) from error


def _observe_source_listing(
    snap_dir: Path,
    directory_fd: int,
    opened_identity: Tuple[int, int, int, int],
) -> Dict[str, os.stat_result]:
    try:
        present = _observe_listing(snap_dir, directory_fd, opened_identity)
    except SnapshotImportPlanError as error:
        raise _from_import_error(error) from error
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _from_export_error(error) from error
    _reject_hardlink_anomalies(present, snap_dir)
    return present


def _observe_held_source_snapshot(
    root: Path,
    requested: str,
    snapshots_fd: int,
    snapshots_identity: Tuple[int, int, int, int, int],
) -> Dict[str, Any]:
    _require_managed_graph(root)
    _observe_directory(
        root / "snapshots", snapshots_fd, snapshots_identity, label="snapshots directory"
    )
    first = _capture_source_tokens(root, requested=requested, resolved=None)
    if not isinstance(first["current_value"], str) or not is_published_snapshot_id(
        first["current_value"]
    ):
        raise SnapshotTransferPlanIntegrityError(
            f"current is not a canonical published snapshot id: {first['current_value']!r}"
        )
    if first["current_value"] not in first["published"]:
        raise SnapshotTransferPlanIntegrityError(
            "current is not a member of the published snapshot set"
        )
    if requested == CURRENT_REF:
        resolved = str(first["current_value"])
    else:
        resolved = requested
        if is_staging_snapshot_name(resolved):
            raise SnapshotTransferPlanError(
                f"staging path is not a published snapshot: {resolved!r}"
            )
        if resolved not in first["published"]:
            raise SnapshotTransferPlanError(
                f"snapshot is not a retained published directory: {resolved!r}"
            )
    first["selected_identity"] = _selected_directory_identity(root, resolved)
    _after_transfer_source_tokens_captured(root, first)
    snap_dir = root / "snapshots" / resolved
    try:
        directory_fd, opened_identity = _open_directory_nofollow(
            snap_dir, label="selected snapshot directory"
        )
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _from_export_error(error) from error
    payload_fds: Dict[str, int] = {}
    try:
        opened = os.fstat(directory_fd)
        complete_dir = _complete_directory_identity(opened)
        if complete_dir != first["selected_identity"]:
            raise SnapshotTransferPlanIntegrityError(
                "selected snapshot directory changed before payload inspection"
            )
        _observe_directory(
            snap_dir, directory_fd, complete_dir, label="selected snapshot directory"
        )
        first_present = _observe_source_listing(snap_dir, directory_fd, opened_identity)
        _after_transfer_source_listed(root, first_present)
        if MANIFEST_NAME not in first_present:
            raise SnapshotTransferPlanIntegrityError(
                "selected snapshot is missing required payload manifest.json"
            )

        identities: Dict[str, Tuple[int, int, int, int, int, int]] = {}
        revisions: Dict[str, str] = {}
        sizes: Dict[str, int] = {}
        for name in _byte_sort(list(first_present)):
            fd, identity, revision, size = _open_held_source_payload(
                directory_fd,
                name,
                snap_dir / name,
                first_present[name],
            )
            payload_fds[name] = fd
            identities[name] = identity
            revisions[name] = revision
            sizes[name] = size

        records = [
            {
                "path": name,
                "size_bytes": sizes[name],
                "content_revision": revisions[name],
            }
            for name in _byte_sort(list(first_present))
        ]
        _after_transfer_source_first_observation(root, records)

        second_dir = _observe_directory(
            snap_dir, directory_fd, complete_dir, label="selected snapshot directory"
        )
        second_present = _observe_source_listing(snap_dir, directory_fd, opened_identity)
        if (
            second_dir != complete_dir
            or _listing_token(first_present) != _listing_token(second_present)
            or set(second_present) != set(payload_fds)
        ):
            raise SnapshotTransferPlanIntegrityError(
                "selected snapshot listing or payload set changed during transfer plan"
            )
        for name in _byte_sort(list(payload_fds)):
            _reobserve_payload(
                directory_fd,
                name,
                snap_dir / name,
                payload_fds[name],
                identities[name],
                revisions[name],
            )
        second = _capture_source_tokens(root, requested=requested, resolved=resolved)
        if (
            first["lock_identity"] != second["lock_identity"]
            or first["current_value"] != second["current_value"]
            or first["current_identity"] != second["current_identity"]
            or first["listing"] != second["listing"]
            or first["selected_identity"] != second["selected_identity"]
            or first["published"] != second["published"]
        ):
            raise SnapshotTransferPlanIntegrityError(
                "publication lock, current, snapshots listing, or selected "
                "snapshot changed during transfer plan"
            )

        try:
            manifest_bytes = _read_held_bytes(
                payload_fds[MANIFEST_NAME],
                path=snap_dir / MANIFEST_NAME,
                label="manifest",
                max_bytes=MAX_MANIFEST_BYTES,
            )
        except SnapshotImportPlanError as error:
            raise _from_import_error(error) from error
        try:
            manifest = _parse_manifest(manifest_bytes, snap_dir / MANIFEST_NAME)
        except SnapshotImportPlanError as error:
            raise _from_import_error(error) from error
        try:
            envelope_id = _validate_source_envelope(
                snap_dir, payload_fds, first_present, manifest
            )
        except SnapshotImportPlanError as error:
            raise _from_import_error(error) from error
        if envelope_id != resolved:
            raise SnapshotTransferPlanIntegrityError(
                "manifest.id differs from the selected snapshot directory"
            )
        planned_records = [
            {
                "path": name,
                "size_bytes": sizes[name],
                "content_revision": revisions[name],
            }
            for name in _byte_sort(list(first_present))
        ]
        source_export_revision = export_revision_of(
            {
                "schema_version": PLAN_SCHEMA_VERSION,
                "resolved_snapshot": resolved,
                "files": planned_records,
            }
        )
        return {
            "root": root,
            "requested": requested,
            "snapshot_id": resolved,
            "snap_dir": snap_dir,
            "directory_fd": directory_fd,
            "directory_identity": complete_dir,
            "opened_identity": opened_identity,
            "payload_fds": payload_fds,
            "payload_identities": identities,
            "payload_revisions": revisions,
            "listing_token": _listing_token(first_present),
            "files": planned_records,
            "source_export_revision": source_export_revision,
            "tokens": first,
            "current": first["current_value"],
            "published_snapshots": list(first["published"]),
        }
    except Exception:
        for fd in payload_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        os.close(directory_fd)
        raise


def _reobserve_held_source(source: Mapping[str, Any]) -> None:
    root = Path(source["root"])
    snap_dir = Path(source["snap_dir"])
    directory_fd = int(source["directory_fd"])
    directory_identity = source["directory_identity"]
    opened_identity = source["opened_identity"]
    payload_fds = source["payload_fds"]
    identities = source["payload_identities"]
    revisions = source["payload_revisions"]
    _observe_directory(
        snap_dir, directory_fd, directory_identity, label="selected snapshot directory"
    )
    present = _observe_source_listing(snap_dir, directory_fd, opened_identity)
    if _listing_token(present) != source["listing_token"] or set(present) != set(
        payload_fds
    ):
        raise SnapshotTransferPlanIntegrityError(
            "selected snapshot listing or payload set changed during target observation"
        )
    for name in _byte_sort(list(payload_fds)):
        _reobserve_payload(
            directory_fd,
            name,
            snap_dir / name,
            payload_fds[name],
            identities[name],
            revisions[name],
        )
        _after_transfer_source_final_payload_recheck(root, name)
    final_present = _observe_source_listing(
        snap_dir, directory_fd, opened_identity
    )
    if (
        _listing_token(final_present) != source["listing_token"]
        or set(final_present) != set(payload_fds)
    ):
        raise SnapshotTransferPlanIntegrityError(
            "selected snapshot listing or payload changed during the final "
            "source recheck"
        )
    later = _capture_source_tokens(
        root, requested=str(source["requested"]), resolved=str(source["snapshot_id"])
    )
    first = source["tokens"]
    if (
        first["lock_identity"] != later["lock_identity"]
        or first["current_value"] != later["current_value"]
        or first["current_identity"] != later["current_identity"]
        or first["listing"] != later["listing"]
        or first["selected_identity"] != later["selected_identity"]
        or first["published"] != later["published"]
        or later["selected_identity"] != directory_identity
    ):
        raise SnapshotTransferPlanIntegrityError(
            "publication lock, current, snapshots listing, or selected "
            "snapshot changed during transfer plan"
        )
    if str(source["requested"]) == CURRENT_REF and later["current_value"] != source[
        "snapshot_id"
    ]:
        raise SnapshotTransferPlanIntegrityError(
            "current no longer names the selected snapshot"
        )
    _observe_directory(
        snap_dir, directory_fd, directory_identity, label="selected snapshot directory"
    )


def _observe_fresh_transfer_plan(
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
    requested: str,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Build one schema-1 transfer plan from already-held descriptors.

    Caller must already hold the two-graph leases. Source payload
    descriptors stay open on success; the caller closes them. This is
    the same decision contract the public plan command emits and is not
    a second path-based CLI invocation.
    """
    _observe_directory(
        source_path, source_fd, source_identity, label="source graph root"
    )
    _observe_directory(
        target_path, target_fd, target_identity, label="target graph root"
    )
    _observe_directory(
        source_snapshots_path,
        source_snapshots_fd,
        source_snapshots_identity,
        label="snapshots directory",
    )
    _observe_directory(
        target_snapshots_path,
        target_snapshots_fd,
        target_snapshots_identity,
        label="snapshots directory",
    )
    source = _observe_held_source_snapshot(
        source_path,
        requested,
        source_snapshots_fd,
        source_snapshots_identity,
    )
    try:
        _observe_directory(
            target_path, target_fd, target_identity, label="target graph root"
        )
        _observe_directory(
            target_snapshots_path,
            target_snapshots_fd,
            target_snapshots_identity,
            label="snapshots directory",
        )
        target = _observe_target_unlocked(target_path, source["snapshot_id"])
        _observe_directory(
            target_path, target_fd, target_identity, label="target graph root"
        )
        _after_transfer_before_source_final_recheck(source_path, target_path)
        _reobserve_held_source(source)
        _observe_directory(
            source_path, source_fd, source_identity, label="source graph root"
        )
        _observe_directory(
            source_snapshots_path,
            source_snapshots_fd,
            source_snapshots_identity,
            label="snapshots directory",
        )
        _after_transfer_source_final_recheck(source_path, target_path)
        final_target = _capture_target_tokens(target_path)
        if final_target != target["tokens"]:
            raise SnapshotTransferPlanIntegrityError(
                "publication lock, current, snapshots listing, or "
                "staging changed during transfer plan"
            )
        _observe_directory(
            target_path, target_fd, target_identity, label="target graph root"
        )
        _observe_directory(
            target_snapshots_path,
            target_snapshots_fd,
            target_snapshots_identity,
            label="snapshots directory",
        )
        result = _build_result(source_path, target_path, source, target)
        return result, source, target
    except Exception:
        for fd in source["payload_fds"].values():
            try:
                os.close(fd)
            except OSError:
                pass
        os.close(int(source["directory_fd"]))
        raise


def _build_result(
    source_graph: Path,
    target_graph: Path,
    source: Mapping[str, Any],
    target: Mapping[str, Any],
) -> Dict[str, Any]:
    notices = [dict(notice) for notice in _COMMAND_NOTICES]
    if target["unrelated_staging"]:
        notices.append(dict(_UNRELATED_STAGING_NOTICE))
    files = [dict(item) for item in source["files"]]
    published = list(target["published_snapshots"])
    result: Dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION_TRANSFER,
        "ok": True,
        "source_graph": str(source_graph),
        "target_graph": str(target_graph),
        "requested_snapshot": source["requested"],
        "snapshot_id": source["snapshot_id"],
        "source_current": source["current"],
        "source_published_snapshots": list(source["published_snapshots"]),
        "files": files,
        "file_count": len(files),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in files),
        "source_export_revision": source["source_export_revision"],
        "source_envelope_valid": True,
        "target_current": target["current"],
        "target_published_snapshots": published,
        "target_published_count": len(published),
        "target_staging_name": target["target_staging_name"],
        "target_snapshot_present": target["target_snapshot_present"],
        "target_staging_present": target["target_staging_present"],
        "target_staging_count": target["staging_count"],
        "blocking_reasons": list(target["blocking_reasons"]),
        "transfer_ready": target["transfer_ready"],
        "transfer_performed": False,
        "source_graph_mutated": False,
        "target_graph_mutated": False,
        "fresh_plan_required_before_transfer": True,
        "notices": notices,
    }
    result["transfer_revision"] = transfer_revision_of(result)
    return result


@contextmanager
def _ordered_shared_graph_leases(
    first: Path, second: Path
) -> Iterator[None]:
    """Hold two shared existing-lock leases in the documented global order."""
    with graph_read_lease(first, allow_unlocked_managed=False):
        with graph_read_lease(second, allow_unlocked_managed=False):
            yield


@contextmanager
def _snapshot_transfer_plan_scope(
    source_graph: Path, snapshot: object, target_graph: Path
) -> Iterator[Dict[str, Any]]:
    """Yield one transfer plan while both shared leases and descriptors are held."""
    try:
        requested = _parse_snapshot(snapshot)
    except SnapshotExportPlanError as error:
        wrapped = SnapshotTransferPlanError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        raise wrapped from error
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
    _after_transfer_graphs_identified(source_path, target_path)
    try:
        _require_descriptor_reads()
    except SnapshotExportPlanError as error:
        raise SnapshotTransferPlanError(str(error)) from error
    _require_path_identity(
        source_path, source_path_identity, label="source graph root"
    )
    _require_path_identity(
        target_path, target_path_identity, label="target graph root"
    )
    source_path, source_fd, source_identity, _source_opened = _open_anchored_directory(
        source_path, source_path_identity, label="source graph root"
    )
    try:
        target_path, target_fd, target_identity, _target_opened = (
            _open_anchored_directory(
                target_path, target_path_identity, label="target graph root"
            )
        )
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
            try:
                with _ordered_shared_graph_leases(first_root, second_root):
                    _observe_directory(
                        source_path,
                        source_fd,
                        source_identity,
                        label="source graph root",
                    )
                    _observe_directory(
                        target_path,
                        target_fd,
                        target_identity,
                        label="target graph root",
                    )
                    (
                        _source_snapshots,
                        source_snapshots_fd,
                        source_snapshots_identity,
                        _source_snapshots_opened,
                    ) = _open_snapshots_directory(
                        source_path, source_fd, source_identity
                    )
                    try:
                        (
                            _target_snapshots,
                            target_snapshots_fd,
                            target_snapshots_identity,
                            _target_snapshots_opened,
                        ) = _open_snapshots_directory(
                            target_path, target_fd, target_identity
                        )
                        try:
                            result, source, _target = _observe_fresh_transfer_plan(
                                source_path,
                                source_fd,
                                source_identity,
                                _source_snapshots,
                                source_snapshots_fd,
                                source_snapshots_identity,
                                target_path,
                                target_fd,
                                target_identity,
                                _target_snapshots,
                                target_snapshots_fd,
                                target_snapshots_identity,
                                requested,
                            )
                            try:
                                _after_transfer_result_ready(
                                    source_path,
                                    target_path,
                                    source_fd,
                                    target_fd,
                                    source_snapshots_fd,
                                    target_snapshots_fd,
                                    int(source["directory_fd"]),
                                    source["payload_fds"],
                                    result,
                                )
                                yield result
                            finally:
                                for fd in source["payload_fds"].values():
                                    try:
                                        os.close(fd)
                                    except OSError:
                                        pass
                                os.close(int(source["directory_fd"]))
                        finally:
                            os.close(target_snapshots_fd)
                    finally:
                        os.close(source_snapshots_fd)
            except SnapshotTransferPlanError:
                raise
            except SnapshotStagingError as error:
                raise _wrap_staging_error(error) from error
            except ByogPublicationLockError as error:
                raise _lock_error(error) from error
            except ByogReaderLockError as error:
                raise _lock_error(error) from error
        finally:
            os.close(target_fd)
    finally:
        os.close(source_fd)


def snapshot_transfer_plan(
    source_graph: Path, snapshot: str, target_graph: Path
) -> Dict[str, Any]:
    """Build one read-only transfer plan without writing files or streams."""
    with _snapshot_transfer_plan_scope(
        source_graph, snapshot, target_graph
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report a read-only plan for transferring one retained snapshot "
            "from one managed BYOG graph to a different managed BYOG graph. "
            "Does not export, import, copy, activate, or mutate either graph. "
            "This plan is not a backup and is not authorization to transfer "
            "or delete anything. Never creates .publish.lock, and is not an "
            "MCP tool. A fresh plan is required before any later transfer."
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
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_transfer_plan_scope(
            args.source_graph, args.snapshot, args.target_graph
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing either graph lease
            # and the held descriptors so the complete response is handed
            # to the caller under that protected interval.
            sys.stdout.flush()
    except SnapshotTransferPlanError as error:
        print(f"snapshot-transfer-plan: {error}", file=sys.stderr)
        return error.exit_code
    except (
        OSError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ) as error:
        print(f"snapshot-transfer-plan: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
