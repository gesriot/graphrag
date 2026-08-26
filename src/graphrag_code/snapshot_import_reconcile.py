#!/usr/bin/env python
"""Read-only snapshot import reconciliation.

``snapshot-import-reconcile`` inspects the aftermath of a complete,
partial, interrupted, or externally modified import attempt. It
reconciles one saved schema-1 ``snapshot-import-plan``, an optional
saved schema-1 ``snapshot-import-apply`` result, and the current state
of the target managed graph. It is observation-only. It does not
retry, recover, repair, copy, publish, activate, pin, prune, clean
staging, run retention, or mutate the graph or the standalone export.

Saved plan and apply-result paths are relative to the invoking cwd
unless absolute. Only bounded regular files are accepted; they are
opened read-only without following symlinks. The conservative limit is
``MAX_INPUT_BYTES`` (1 MiB). Complete input validation finishes before
the graph is observed. Malformed, oversized, symlinked, replaced,
truncated, or structurally invalid inputs fail with exit 2 and empty
stdout. A structurally valid apply result that refers to another plan,
graph, export, or snapshot is an integrity failure: exit 1 and empty
stdout. A saved apply result is impossible for a blocked saved plan.

The command requires an existing real managed ``current + snapshots/``
graph with an already-existing regular ``.publish.lock``. It never
creates, adopts, truncates, chmods, or replaces that lock. One shared
existing-lock lease covers target observation, result construction,
serialization, stdout write, and stdout flush. Graph-root and
snapshots-directory descriptors stay anchored with no-follow opens and
complete identity checks. The command does not inspect operator pins,
claim pins, retention configuration, or the standalone export
directory, and it does not create a nested observation window or
invoke a public CLI.

A present retained snapshot is hashed twice initially through held
descriptors using the existing import/export hashing and
persisted-envelope contracts, then receives a final complete
held-payload recheck after target-state observation. A stable valid
revision mismatch is a completed observation. An invalid or
concurrently changing retained snapshot is an integrity failure.
Snapshot absence, staging presence, current drift, and extra or
missing retained snapshots are all normal completed observations.
``ok=true`` means this read-only
reconciliation succeeded; it does not mean import apply succeeded.
A fresh import plan is still required before any later apply. MCP
stays exactly 13 read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-import-reconcile --graph <managed-root> \\
        --plan-file <saved-import-plan.json> \\
        [--apply-result-file <saved-import-apply-result.json>] [--json]
    python -m graphrag_code.snapshot_import_reconcile --graph <managed-root> \\
        --plan-file <saved-import-plan.json> \\
        [--apply-result-file <saved-import-apply-result.json>] [--json]
    uv run python scripts/snapshot_import_reconcile.py --graph <managed-root> \\
        --plan-file <saved-import-plan.json> \\
        [--apply-result-file <saved-import-apply-result.json>] [--json]
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
    graph_read_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
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
    _from_export_error,
    _lock_error as _import_lock_error,
    _observe_held_directory,
    _observe_listing,
    _open_anchored_graph,
    _open_held_payload,
    _parse_manifest,
    _read_held_bytes,
    _reobserve_held_payload,
    _require_managed_graph as _import_require_managed_graph,
    _require_path_identity,
    _resolve_graph_root as _import_resolve_graph_root,
    _validate_source_envelope,
    _wrap_staging_error as _import_wrap_staging_error,
    canonical_import_revision_payload,
    import_revision_of,
)
from graphrag_code.snapshot_staging import (
    MAX_CURRENT_BYTES,
    MAX_PUBLISHED_SNAPSHOTS,
    MAX_STAGING_ENTRIES,
    SnapshotStagingError,
    SnapshotStagingIntegrityError,
)


RECONCILE_SCHEMA_VERSION = 1
PLAN_SCHEMA_VERSION_IMPORT = 1
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
            "snapshot-import-reconcile is observation-only. ok means this "
            "read completed, not that snapshot-import-apply succeeded, that "
            "the snapshot exists, that it matches, that staging was cleaned, "
            "or that current stayed unchanged."
        ),
    },
    {
        "code": "absence_does_not_prove_apply_failed",
        "kind": "notice",
        "message": (
            "Snapshot absence does not prove snapshot-import-apply failed "
            "or that another actor deleted it."
        ),
    },
    {
        "code": "presence_does_not_prove_apply_created",
        "kind": "notice",
        "message": (
            "Snapshot presence does not prove snapshot-import-apply created "
            "it. creation_cause_proven is always false."
        ),
    },
    {
        "code": "revision_equality_is_observation_window_only",
        "kind": "notice",
        "message": (
            "Matching revision proves only payload-contract equality during "
            "the bounded observation window."
        ),
    },
    {
        "code": "staging_presence_does_not_prove_apply_left_it",
        "kind": "notice",
        "message": (
            "Exact staging presence does not prove snapshot-import-apply "
            "left it."
        ),
    },
    {
        "code": "staging_absence_does_not_prove_apply_cleaned_it",
        "kind": "notice",
        "message": (
            "Exact staging absence does not prove snapshot-import-apply "
            "cleaned it."
        ),
    },
    {
        "code": "snapshot_active_is_not_activation",
        "kind": "notice",
        "message": (
            "snapshot_active means only that current currently names "
            "snapshot_id. It does not prove snapshot-import-apply activated "
            "it; that command never activates."
        ),
    },
    {
        "code": "saved_apply_result_is_declaration_only",
        "kind": "notice",
        "message": (
            "A saved complete or partial apply result is a declaration being "
            "compared, not independently authenticated provenance."
        ),
    },
    {
        "code": "source_export_not_observed",
        "kind": "notice",
        "message": (
            "The source standalone export is not observed and may have "
            "changed or disappeared."
        ),
    },
    {
        "code": "fresh_plan_required_before_import",
        "kind": "notice",
        "message": (
            "A fresh import plan is required before any later apply. This "
            "reconciliation is not a retry token."
        ),
    },
    {
        "code": "no_recovery_performed",
        "kind": "notice",
        "message": (
            "recovery_performed is always false. Reconciliation performs no "
            "retry, recovery, restore, activation, deletion, cleanup, "
            "repair, pinning, pruning, or retention."
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
            "snapshot-import-reconcile is CLI-only and intentionally "
            "absent from the fixed 13-tool MCP set."
        ),
    },
)


class SnapshotImportReconcileError(Exception):
    """Malformed arguments, inputs, or unsupported invocation. Default exit 2."""

    exit_code = 2


class SnapshotImportReconcileIntegrityError(SnapshotImportReconcileError):
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
    observed = result.get("observed_snapshot_export_revision")
    if observed is None:
        observed_text = "null"
    else:
        observed_text = str(observed)
    return (
        "snapshot-import-reconcile: "
        f"graph={result.get('graph')} "
        f"snapshot_id={result.get('snapshot_id')} "
        f"published_snapshot_state={result.get('published_snapshot_state')} "
        f"input_import_revision={result.get('input_import_revision')} "
        f"observed_snapshot_export_revision={observed_text} "
        "snapshot_matches_plan="
        f"{str(bool(result.get('snapshot_matches_plan'))).lower()} "
        f"current={result.get('current')} "
        "snapshot_active="
        f"{str(bool(result.get('snapshot_active'))).lower()} "
        "target_staging_present="
        f"{str(bool(result.get('target_staging_present'))).lower()} "
        f"declared_apply_outcome={result.get('declared_apply_outcome')} "
        f"ok={str(bool(result.get('ok'))).lower()} "
        "This reconciliation is observation-only and is not authorization "
        "to import, activate, or delete anything."
    )


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant {value!r}")


def _require_revision(value: object, label: str) -> str:
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise SnapshotImportReconcileError(
            f"{label} must be sha256:<64 lowercase hex>, got {value!r}"
        )
    return value


def _require_bool(
    value: object, label: str, *, expected: Optional[bool] = None
) -> bool:
    if type(value) is not bool:
        raise SnapshotImportReconcileError(f"{label} must be a JSON boolean")
    if expected is not None and value is not expected:
        raise SnapshotImportReconcileError(
            f"{label} must be {str(expected).lower()}"
        )
    return value


def _require_non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotImportReconcileError(f"{label} must be a non-empty string")
    return value


def _require_absolute_string(value: object, label: str) -> str:
    text = _require_non_empty_string(value, label)
    if not Path(text).is_absolute():
        raise SnapshotImportReconcileError(f"{label} must be an absolute path")
    return text


def _require_canonical_snapshot_id(value: object, label: str) -> str:
    text = _require_non_empty_string(value, label)
    if not _is_canonical_direct_name(text) or not is_published_snapshot_id(text):
        raise SnapshotImportReconcileError(
            f"{label} is not a canonical published snapshot id"
        )
    return text


def _after_input_path_lstat(_path: Path) -> None:
    """Test hook after lstat and before the no-follow input open."""
    return None


def _after_input_file_read(_path: Path, _digest: str) -> None:
    """Test hook after the first complete input read and before the recheck."""
    return None


def _after_graph_path_inspected(_graph: Path) -> None:
    """Test hook after initial graph-path validation and before the lease."""
    return None


def _after_snapshots_path_inspected(_graph_path: Path, _graph_fd: int) -> None:
    """Test hook after snapshots lstat and before the snapshots open."""
    return None


def _after_first_target_scan(_root: Path, _scan: Mapping[str, Any]) -> None:
    """Test hook after the first graph scan and before the second."""
    return None


def _after_second_target_scan(_root: Path, _scan: Mapping[str, Any]) -> None:
    """Test hook after the second graph scan and before the final payload recheck."""
    return None


def _after_target_snapshot_first_stat(
    _path: Path, _info: Optional[os.stat_result]
) -> None:
    """Test hook after the first descriptor-relative target snapshot stat."""
    return None


def _after_target_snapshot_opened(
    _path: Path, _directory_fd: int, _identity: Tuple[int, int, int, int, int]
) -> None:
    """Test hook after the target snapshot directory is anchored."""
    return None


def _after_first_snapshot_observation(
    _path: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after the first complete target-snapshot observation."""
    return None


def _after_second_listed(
    _path: Path, _entries: Mapping[str, os.stat_result]
) -> None:
    """Test hook after the second-pass snapshot listing."""
    return None


def _after_target_staging_first_stat(
    _path: Path, _info: Optional[os.stat_result]
) -> None:
    """Test hook after the first descriptor-relative exact staging stat."""
    return None


def _after_result_ready(
    _graph: Path,
    _graph_fd: int,
    _snapshots_fd: int,
    _snapshot_fd: Optional[int],
    _payload_fds: Mapping[str, int],
    _result: Mapping[str, Any],
) -> None:
    """Test hook after the result is built and before it is yielded."""
    return None


def _wrap_plan_error(error: Exception) -> SnapshotImportReconcileError:
    if isinstance(error, SnapshotImportPlanIntegrityError):
        return SnapshotImportReconcileIntegrityError(str(error))
    if isinstance(error, SnapshotImportPlanError):
        wrapped = SnapshotImportReconcileError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    if isinstance(error, SnapshotExportPlanIntegrityError):
        return SnapshotImportReconcileIntegrityError(str(error))
    if isinstance(error, SnapshotExportPlanError):
        converted = _from_export_error(error)
        return _wrap_plan_error(converted)
    if isinstance(error, SnapshotStagingIntegrityError):
        return SnapshotImportReconcileIntegrityError(str(error))
    if isinstance(error, SnapshotStagingError):
        converted = _import_wrap_staging_error(error)
        return _wrap_plan_error(converted)
    return SnapshotImportReconcileError(str(error))


def _lock_error(error: Exception) -> SnapshotImportReconcileError:
    wrapped = _import_lock_error(error)
    if isinstance(wrapped, SnapshotImportPlanIntegrityError):
        return SnapshotImportReconcileIntegrityError(str(wrapped))
    if "publication lock is missing" in str(error):
        return SnapshotImportReconcileError(f"{error}\n{_MISSING_LOCK_HINT}")
    out = SnapshotImportReconcileError(str(wrapped))
    out.exit_code = getattr(wrapped, "exit_code", 2)
    return out


def _require_managed_graph(root: Path) -> None:
    try:
        _import_require_managed_graph(root)
    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error


def _resolve_graph_root(
    graph: Path,
) -> Tuple[Path, Tuple[int, int, int, int, int]]:
    try:
        path, identity = _import_resolve_graph_root(graph)
    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error
    return path, identity


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
        raise SnapshotImportReconcileError(
            f"{label} does not exist: {resolved}"
        ) from error
    except OSError as error:
        raise SnapshotImportReconcileError(
            f"cannot inspect {label} {resolved}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotImportReconcileError(
            f"{label} must be a regular file, not a symlink: {resolved}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotImportReconcileError(
            f"{label} is not a regular file: {resolved}"
        )
    if before.st_size > MAX_INPUT_BYTES:
        raise SnapshotImportReconcileError(
            f"{label} exceeds bound {MAX_INPUT_BYTES} bytes: {resolved}"
        )
    if not hasattr(os, "O_NOFOLLOW"):
        raise SnapshotImportReconcileError(
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
            raise SnapshotImportReconcileError(
                f"{label} changed or became unsafe while opening it: {resolved}"
            ) from error
        raise SnapshotImportReconcileError(
            f"cannot safely open {label} {resolved}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = resolved.lstat()
        except OSError as error:
            raise SnapshotImportReconcileError(
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
            raise SnapshotImportReconcileError(
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
            raise SnapshotImportReconcileError(
                f"{label} exceeds bound {MAX_INPUT_BYTES} bytes: {resolved}"
            )
        after_fd = os.fstat(fd)
        try:
            after_path = resolved.lstat()
        except OSError as error:
            raise SnapshotImportReconcileError(
                f"{label} changed while it was read: {resolved}"
            ) from error
        if (
            _complete_input_identity(after_fd) != before_id
            or stat.S_ISLNK(after_path.st_mode)
            or not stat.S_ISREG(after_path.st_mode)
            or _complete_input_identity(after_path) != before_id
            or len(data) != opened.st_size
        ):
            raise SnapshotImportReconcileError(
                f"{label} changed while it was read: {resolved}"
            )
        digest = _hash_bytes(data)
        _after_input_file_read(resolved, digest)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError as error:
            raise SnapshotImportReconcileError(
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
            raise SnapshotImportReconcileError(
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
            raise SnapshotImportReconcileError(
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
        raise SnapshotImportReconcileError(
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
        raise SnapshotImportReconcileError(
            f"{label} is not valid JSON: {error}"
        ) from error
    if not isinstance(parsed, dict):
        raise SnapshotImportReconcileError(f"{label} must be a JSON object")
    return resolved, parsed


def _file_records(value: object, *, label: str, snapshot_id: str) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise SnapshotImportReconcileError(f"{label} must be an array")
    try:
        canonical = canonical_export_revision_payload(
            {
                "schema_version": PLAN_SCHEMA_VERSION,
                "resolved_snapshot": snapshot_id,
                "files": value,
            }
        )
    except SnapshotExportPlanError as error:
        raise SnapshotImportReconcileError(f"{label}: {error}") from error
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
        raise SnapshotImportReconcileError(
            f"{label} is missing required envelope payload {missing[0]}"
        )
    extra = [name for name in names if name not in ACCEPTED_PAYLOAD_FILES]
    if extra:
        raise SnapshotImportReconcileError(
            f"{label} contains an unexpected envelope name: {extra[0]!r}"
        )
    if list(names) != _byte_sort(names):
        raise SnapshotImportReconcileError(
            f"{label} must be sorted in UTF-8-byte order"
        )
    return records


def _validate_saved_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    schema = plan.get("schema_version")
    if isinstance(schema, bool) or schema != PLAN_SCHEMA_VERSION_IMPORT:
        raise SnapshotImportReconcileError("saved plan schema_version must be 1")
    _require_bool(plan.get("ok"), "saved plan ok", expected=True)
    graph = _require_absolute_string(plan.get("graph"), "saved plan graph")
    export_directory = _require_absolute_string(
        plan.get("export_directory"), "saved plan export_directory"
    )
    snapshot_id = _require_canonical_snapshot_id(
        plan.get("snapshot_id"), "saved plan snapshot_id"
    )
    records = _file_records(
        plan.get("files"), label="saved plan files", snapshot_id=snapshot_id
    )
    file_count = plan.get("file_count")
    if isinstance(file_count, bool) or file_count != len(records):
        raise SnapshotImportReconcileError(
            "saved plan file_count must equal the number of file records"
        )
    total = plan.get("total_size_bytes")
    expected_total = sum(int(item["size_bytes"]) for item in records)
    if isinstance(total, bool) or total != expected_total:
        raise SnapshotImportReconcileError(
            "saved plan total_size_bytes must equal the sum of file sizes"
        )
    _require_bool(
        plan.get("source_envelope_valid"),
        "saved plan source_envelope_valid",
        expected=True,
    )
    current = _require_canonical_snapshot_id(plan.get("current"), "saved plan current")
    published = plan.get("published_snapshots")
    if not isinstance(published, list) or any(
        not isinstance(item, str) for item in published
    ):
        raise SnapshotImportReconcileError(
            "saved plan published_snapshots must be a list of snapshot ids"
        )
    published_ids = list(published)
    if any(not is_published_snapshot_id(item) for item in published_ids):
        raise SnapshotImportReconcileError(
            "saved plan published_snapshots contains a non-canonical id"
        )
    if (
        len(set(published_ids)) != len(published_ids)
        or published_ids != _byte_sort(published_ids)
    ):
        raise SnapshotImportReconcileError(
            "saved plan published_snapshots must be unique and sorted in "
            "UTF-8-byte order"
        )
    if current not in published_ids:
        raise SnapshotImportReconcileError(
            "saved plan current is not a member of published_snapshots"
        )
    if len(published_ids) > MAX_PUBLISHED_SNAPSHOTS:
        raise SnapshotImportReconcileError(
            "saved plan published snapshot count exceeds bound "
            f"{MAX_PUBLISHED_SNAPSHOTS}"
        )
    published_count = plan.get("published_count")
    if isinstance(published_count, bool) or published_count != len(published_ids):
        raise SnapshotImportReconcileError(
            "saved plan published_count must equal published_snapshots"
        )
    staging_name = plan.get("target_staging_name")
    expected_staging = f"{STAGING_NAME_PREFIX}{snapshot_id}"
    if (
        not isinstance(staging_name, str)
        or not _is_canonical_direct_name(staging_name)
        or staging_name != expected_staging
    ):
        raise SnapshotImportReconcileError(
            "saved plan target_staging_name must be exactly .staging-<snapshot-id>"
        )
    _require_bool(
        plan.get("import_performed"), "saved plan import_performed", expected=False
    )
    _require_bool(
        plan.get("graph_mutated"), "saved plan graph_mutated", expected=False
    )
    _require_bool(
        plan.get("export_mutated"), "saved plan export_mutated", expected=False
    )
    _require_bool(
        plan.get("fresh_plan_required_before_import"),
        "saved plan fresh_plan_required_before_import",
        expected=True,
    )
    notices = plan.get("notices")
    if not isinstance(notices, list):
        raise SnapshotImportReconcileError("saved plan notices must be an array")
    staging_count = plan.get("staging_count")
    if (
        isinstance(staging_count, bool)
        or not isinstance(staging_count, int)
        or staging_count < 0
        or staging_count > MAX_STAGING_ENTRIES
    ):
        raise SnapshotImportReconcileError(
            "saved plan staging_count must be a non-negative integer no greater "
            f"than {MAX_STAGING_ENTRIES}"
        )
    target_staging_present = plan.get("target_staging_present")
    if type(target_staging_present) is not bool:
        raise SnapshotImportReconcileError(
            "saved plan target_staging_present must be a JSON boolean"
        )
    if target_staging_present is True and staging_count < 1:
        raise SnapshotImportReconcileError(
            "saved plan staging_count is inconsistent with target_staging_present"
        )
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
        raise SnapshotImportReconcileError(str(error)) from error
    if recomputed_source != declared_source:
        raise SnapshotImportReconcileError(
            "saved plan source_export_revision does not match the canonical "
            "snapshot-export-plan contract"
        )
    revision_inputs = {
        "schema_version": PLAN_SCHEMA_VERSION_IMPORT,
        "snapshot_id": snapshot_id,
        "source_export_revision": declared_source,
        "current": current,
        "published_snapshots": published_ids,
        "target_staging_name": staging_name,
        "target_snapshot_present": plan.get("target_snapshot_present"),
        "target_staging_present": target_staging_present,
        "blocking_reasons": plan.get("blocking_reasons"),
        "import_ready": plan.get("import_ready"),
        "source_envelope_valid": True,
        "import_performed": False,
        "fresh_plan_required_before_import": True,
    }
    try:
        canonical_import_revision_payload(revision_inputs)
        recomputed_import = import_revision_of(revision_inputs)
    except SnapshotImportPlanError as error:
        raise SnapshotImportReconcileError(f"saved plan: {error}") from error
    declared_import = _require_revision(
        plan.get("import_revision"), "saved plan import_revision"
    )
    if recomputed_import != declared_import:
        raise SnapshotImportReconcileError(
            "saved plan import_revision does not match the canonical "
            "snapshot-import-plan contract"
        )
    return {
        "schema_version": PLAN_SCHEMA_VERSION_IMPORT,
        "ok": True,
        "graph": graph,
        "export_directory": export_directory,
        "snapshot_id": snapshot_id,
        "source_export_revision": declared_source,
        "files": records,
        "file_count": len(records),
        "total_size_bytes": expected_total,
        "source_envelope_valid": True,
        "current": current,
        "published_snapshots": published_ids,
        "published_count": len(published_ids),
        "target_staging_name": staging_name,
        "target_snapshot_present": bool(revision_inputs["target_snapshot_present"]),
        "target_staging_present": target_staging_present,
        "staging_count": staging_count,
        "blocking_reasons": list(revision_inputs["blocking_reasons"]),
        "import_ready": bool(revision_inputs["import_ready"]),
        "import_revision": declared_import,
        "import_performed": False,
        "graph_mutated": False,
        "export_mutated": False,
        "fresh_plan_required_before_import": True,
        "notices": notices,
    }


def _optional_current_after(value: object, label: str) -> Optional[str]:
    if value is None:
        return None
    return _require_canonical_snapshot_id(value, label)


def _validate_apply_outcome(result: Mapping[str, Any]) -> str:
    ok = _require_bool(result.get("ok"), "saved apply result ok")
    partial = _require_bool(result.get("partial"), "saved apply result partial")
    import_confirmed = _require_bool(
        result.get("import_confirmed"), "saved apply result import_confirmed"
    )
    import_performed = _require_bool(
        result.get("import_performed"), "saved apply result import_performed"
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
    current_unchanged = _require_bool(
        result.get("current_unchanged"), "saved apply result current_unchanged"
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
        result.get("snapshots_fsync_confirmed"),
        "saved apply result snapshots_fsync_confirmed",
    )
    filesystem_may_have_changed = _require_bool(
        result.get("filesystem_may_have_changed"),
        "saved apply result filesystem_may_have_changed",
    )
    retry_requires_fresh_plan = _require_bool(
        result.get("retry_requires_fresh_plan"),
        "saved apply result retry_requires_fresh_plan",
    )
    graph_mutated = _require_bool(
        result.get("graph_mutated"), "saved apply result graph_mutated"
    )
    export_mutated = _require_bool(
        result.get("export_mutated"), "saved apply result export_mutated", expected=False
    )
    activation_performed = _require_bool(
        result.get("activation_performed"),
        "saved apply result activation_performed",
        expected=False,
    )
    retention_performed = _require_bool(
        result.get("retention_performed"),
        "saved apply result retention_performed",
        expected=False,
    )
    error = result.get("error")
    if error is not None and (not isinstance(error, str) or error == ""):
        raise SnapshotImportReconcileError(
            "saved apply result error must be a non-empty string or null"
        )
    current_before = _require_canonical_snapshot_id(
        result.get("current_before"), "saved apply result current_before"
    )
    current_after = _optional_current_after(
        result.get("current_after"), "saved apply result current_after"
    )
    if current_unchanged is True and current_after != current_before:
        raise SnapshotImportReconcileError(
            "saved apply result current_unchanged is true only when "
            "current_after equals current_before"
        )
    complete = (
        ok is True
        and partial is False
        and import_confirmed is True
        and import_performed is True
        and publication_attempted is True
        and publication_performed is True
        and snapshot_verified is True
        and current_after == current_before
        and current_unchanged is True
        and staging_created is True
        and staging_remaining is False
        and snapshots_fsync_confirmed is True
        and filesystem_may_have_changed is True
        and retry_requires_fresh_plan is False
        and graph_mutated is True
        and staging_cleanup_attempted is False
        and error is None
    )
    pre_publication = (
        ok is False
        and partial is True
        and import_confirmed is True
        and staging_created is True
        and import_performed is False
        and publication_performed is False
        and snapshot_verified is False
        and current_after is None
        and current_unchanged is False
        and snapshots_fsync_confirmed is False
        and filesystem_may_have_changed is True
        and retry_requires_fresh_plan is True
        and graph_mutated is True
        and isinstance(error, str)
        and error != ""
        and (staging_remaining is False) <= (staging_cleanup_attempted is True)
    )
    post_publication = (
        ok is False
        and partial is True
        and import_confirmed is True
        and import_performed is True
        and publication_attempted is True
        and publication_performed is True
        and staging_created is True
        and staging_cleanup_attempted is False
        and filesystem_may_have_changed is True
        and retry_requires_fresh_plan is True
        and graph_mutated is True
        and isinstance(error, str)
        and error != ""
        and not (staging_remaining is True and snapshot_verified is True)
        and not (current_unchanged is True and current_after != current_before)
    )
    if complete:
        return _DECLARED_COMPLETE
    if pre_publication:
        return _DECLARED_PRE_PUBLICATION
    if post_publication:
        return _DECLARED_POST_PUBLICATION
    raise SnapshotImportReconcileError(
        "saved apply result flags are not an exact complete-success, "
        "emitted pre-publication partial, or emitted post-publication "
        "partial outcome"
    )


def _validate_saved_apply_result(
    result: Mapping[str, Any], plan: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    schema = result.get("schema_version")
    if isinstance(schema, bool) or schema != APPLY_SCHEMA_VERSION:
        raise SnapshotImportReconcileError(
            "saved apply result schema_version must be 1"
        )
    outcome = _validate_apply_outcome(result)
    graph = _require_absolute_string(result.get("graph"), "saved apply result graph")
    export_directory = _require_absolute_string(
        result.get("export_directory"), "saved apply result export_directory"
    )
    snapshot_id = _require_canonical_snapshot_id(
        result.get("snapshot_id"), "saved apply result snapshot_id"
    )
    expected = _require_revision(
        result.get("expected_import_revision"),
        "saved apply result expected_import_revision",
    )
    observed = _require_revision(
        result.get("observed_import_revision"),
        "saved apply result observed_import_revision",
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
    file_count = result.get("file_count")
    if isinstance(file_count, bool) or file_count != len(records):
        raise SnapshotImportReconcileError(
            "saved apply result file_count must equal the number of file records"
        )
    total = result.get("total_size_bytes")
    expected_total = sum(int(item["size_bytes"]) for item in records)
    if isinstance(total, bool) or total != expected_total:
        raise SnapshotImportReconcileError(
            "saved apply result total_size_bytes must equal the sum of file sizes"
        )
    current_before = _require_canonical_snapshot_id(
        result.get("current_before"), "saved apply result current_before"
    )
    notices = result.get("notices")
    if not isinstance(notices, list):
        raise SnapshotImportReconcileError(
            "saved apply result notices must be an array"
        )
    if plan.get("import_ready") is not True:
        raise SnapshotImportReconcileIntegrityError(
            "saved apply result is impossible for a blocked saved plan"
        )
    if (
        graph != plan["graph"]
        or export_directory != plan["export_directory"]
        or snapshot_id != plan["snapshot_id"]
        or expected != plan["import_revision"]
        or observed != plan["import_revision"]
        or source_export_revision != plan["source_export_revision"]
        or records != plan["files"]
        or file_count != plan["file_count"]
        or expected_total != plan["total_size_bytes"]
        or current_before != plan["current"]
    ):
        raise SnapshotImportReconcileIntegrityError(
            "saved apply result refers to another plan"
        )
    return outcome, {
        "schema_version": APPLY_SCHEMA_VERSION,
        "graph": graph,
        "export_directory": export_directory,
        "snapshot_id": snapshot_id,
        "expected_import_revision": expected,
        "observed_import_revision": observed,
        "source_export_revision": source_export_revision,
        "planned_files": records,
        "file_count": file_count,
        "total_size_bytes": expected_total,
        "current_before": current_before,
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
        raise SnapshotImportReconcileIntegrityError(
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
        raise SnapshotImportReconcileIntegrityError(
            f"{label} changed during reconciliation: {path}"
        ) from error
    if (
        not stat.S_ISDIR(held.st_mode)
        or _child_inode(held) != expected_inode
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _child_inode(current) != expected_inode
    ):
        raise SnapshotImportReconcileIntegrityError(
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
        raise SnapshotImportReconcileIntegrityError(
            f"{label} must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(child_info.st_mode):
        raise SnapshotImportReconcileIntegrityError(
            f"{label} is not a real directory: {path}"
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise SnapshotImportReconcileIntegrityError(
                f"{label} changed or became unsafe while opening it: {path}"
            ) from error
        raise SnapshotImportReconcileIntegrityError(
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
            raise SnapshotImportReconcileIntegrityError(
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
        raise SnapshotImportReconcileError(
            f"snapshots directory does not exist: {path}"
        ) from error
    except OSError as error:
        raise SnapshotImportReconcileError(
            f"cannot inspect snapshots directory {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotImportReconcileIntegrityError(
            f"snapshots directory must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotImportReconcileIntegrityError(
            f"snapshots directory is not a real directory: {path}"
        )
    before_identity = _complete_directory_identity(before)
    _after_snapshots_path_inspected(graph_path, graph_fd)
    fd, opened_identity = _open_child_directory(
        graph_fd, name, path, before, label="snapshots directory"
    )
    try:
        if opened_identity != before_identity:
            raise SnapshotImportReconcileIntegrityError(
                f"snapshots directory changed or became unsafe while opening it: {path}"
            )
        _observe_held_directory(
            graph_path, graph_fd, graph_identity, label="graph root"
        )
        path_info = path.lstat()
        if (
            stat.S_ISLNK(path_info.st_mode)
            or not stat.S_ISDIR(path_info.st_mode)
            or _complete_directory_identity(path_info) != opened_identity
        ):
            raise SnapshotImportReconcileIntegrityError(
                f"snapshots directory changed or was replaced: {path}"
            )
        return path, fd, opened_identity
    except SnapshotImportPlanError as error:
        os.close(fd)
        raise _wrap_plan_error(error) from error
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
        raise SnapshotImportReconcileIntegrityError(
            f"publication lock disappeared during reconciliation: {path}"
        ) from error
    except OSError as error:
        raise SnapshotImportReconcileIntegrityError(
            f"cannot inspect publication lock {path}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotImportReconcileIntegrityError(
            f"unsafe symlinked publication lock is unsupported: {path}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotImportReconcileIntegrityError(
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
        raise SnapshotImportReconcileIntegrityError(
            f"current pointer disappeared during reconciliation: {path}"
        ) from error
    except OSError as error:
        raise SnapshotImportReconcileIntegrityError(
            f"cannot inspect current pointer {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotImportReconcileIntegrityError(
            f"unsafe symlinked current pointer is unsupported: {path}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotImportReconcileIntegrityError(
            f"current pointer is not a regular file: {path}"
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open("current", flags, dir_fd=graph_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT}:
            raise SnapshotImportReconcileIntegrityError(
                f"current pointer changed or became unsafe while opening it: {path}"
            ) from error
        raise SnapshotImportReconcileError(
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
            raise SnapshotImportReconcileIntegrityError(
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
            raise SnapshotImportReconcileError(
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
            raise SnapshotImportReconcileIntegrityError(
                f"current pointer changed while it was read: {path}"
            )
    finally:
        os.close(fd)
    try:
        current_id = data.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise SnapshotImportReconcileError(
            f"current pointer is not valid UTF-8: {path}"
        ) from error
    if not is_published_snapshot_id(current_id):
        raise SnapshotImportReconcileIntegrityError(
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
        raise SnapshotImportReconcileIntegrityError(
            f"cannot inspect snapshots directory descriptor {snapshots_path}: {error}"
        ) from error
    expected_inode = (snapshots_identity[0], snapshots_identity[1])
    if not stat.S_ISDIR(held.st_mode) or _child_inode(held) != expected_inode:
        raise SnapshotImportReconcileIntegrityError(
            "snapshots directory descriptor changed during reconciliation"
        )
    entries: List[Tuple[str, os.stat_result]] = []
    try:
        with os.scandir(snapshots_fd) as iterator:
            for entry in iterator:
                if len(entries) >= MAX_PUBLISHED_SNAPSHOTS + MAX_STAGING_ENTRIES:
                    raise SnapshotImportReconcileError(
                        "snapshots directory entry count exceeds bound "
                        f"{MAX_PUBLISHED_SNAPSHOTS + MAX_STAGING_ENTRIES}: "
                        f"{snapshots_path}"
                    )
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise SnapshotImportReconcileIntegrityError(
                        f"cannot inspect snapshots child {snapshots_path / entry.name}: "
                        f"{error}"
                    ) from error
                entries.append((entry.name, info))
    except SnapshotImportReconcileError:
        raise
    except OSError as error:
        raise SnapshotImportReconcileIntegrityError(
            f"cannot list snapshots directory {snapshots_path}: {error}"
        ) from error
    after = os.fstat(snapshots_fd)
    if not stat.S_ISDIR(after.st_mode) or _child_inode(after) != expected_inode:
        raise SnapshotImportReconcileIntegrityError(
            "snapshots directory changed while it was listed"
        )
    entries.sort(key=lambda item: item[0].encode("utf-8"))
    published: List[str] = []
    published_identities: Dict[str, Tuple[int, int]] = {}
    target_snapshot: Optional[os.stat_result] = None
    target_staging: Optional[os.stat_result] = None
    for name, info in entries:
        path = snapshots_path / name
        if not _is_canonical_direct_name(name):
            raise SnapshotImportReconcileIntegrityError(
                f"snapshots directory contains a non-canonical name: {path}"
            )
        if stat.S_ISLNK(info.st_mode):
            raise SnapshotImportReconcileIntegrityError(
                f"unsafe symlinked snapshot entry: {path}"
            )
        if name == snapshot_id:
            target_snapshot = info
        if name == staging_name:
            target_staging = info
        if is_staging_snapshot_name(name):
            continue
        if is_published_snapshot_id(name) and stat.S_ISDIR(info.st_mode):
            published.append(name)
            published_identities[name] = _child_inode(info)
            if len(published) > MAX_PUBLISHED_SNAPSHOTS:
                raise SnapshotImportReconcileError(
                    "published snapshot count exceeds bound "
                    f"{MAX_PUBLISHED_SNAPSHOTS}"
                )
            continue
        if name == snapshot_id:
            continue
        raise SnapshotImportReconcileIntegrityError(
            f"unexpected unsafe snapshots entry is not published history: {path}"
        )
    return {
        "published": published,
        "published_identities": published_identities,
        "target_snapshot": target_snapshot,
        "target_staging": target_staging,
        "snapshots_inode": _child_inode(held),
    }


def _child_presence_token(
    info: Optional[os.stat_result], *, require_directory: bool, path: Path, label: str
) -> Optional[Tuple[int, int, int]]:
    if info is None:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotImportReconcileIntegrityError(
            f"{label} must be a real directory, not a symlink: {path}"
        )
    if require_directory and not stat.S_ISDIR(info.st_mode):
        raise SnapshotImportReconcileIntegrityError(
            f"{label} is not a real directory: {path}"
        )
    return (info.st_dev, info.st_ino, info.st_mode)


def _scan_target(
    graph_path: Path,
    graph_fd: int,
    graph_identity: Tuple[int, int, int, int, int],
    snapshots_path: Path,
    snapshots_fd: int,
    snapshots_identity: Tuple[int, int, int, int, int],
    snapshot_id: str,
    staging_name: str,
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
        raise SnapshotImportReconcileIntegrityError(
            "current is not a member of the published snapshot set"
        )
    snapshot_path = snapshots_path / snapshot_id
    staging_path = snapshots_path / staging_name
    snapshot_token = _child_presence_token(
        listing["target_snapshot"],
        require_directory=True,
        path=snapshot_path,
        label="target snapshot",
    )
    staging_token = _child_presence_token(
        listing["target_staging"],
        require_directory=True,
        path=staging_path,
        label="import staging directory",
    )
    return {
        "lock_identity": lock_identity,
        "current": current,
        "current_identity": current_identity,
        "published": list(listing["published"]),
        "published_identities": dict(listing["published_identities"]),
        "target_snapshot": listing["target_snapshot"],
        "target_staging": listing["target_staging"],
        "snapshot_token": snapshot_token,
        "staging_token": staging_token,
        "graph_inode": (graph_identity[0], graph_identity[1]),
        "snapshots_inode": listing["snapshots_inode"],
    }


def _comparable_scan(scan: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "lock_identity": scan["lock_identity"],
        "current": scan["current"],
        "current_identity": scan["current_identity"],
        "published": list(scan["published"]),
        "published_identities": dict(scan["published_identities"]),
        "snapshot_token": scan["snapshot_token"],
        "staging_token": scan["staging_token"],
        "graph_inode": scan["graph_inode"],
        "snapshots_inode": scan["snapshots_inode"],
    }


def _listing_token(
    present: Mapping[str, os.stat_result],
) -> Dict[str, Tuple[int, int, int, int, int, int]]:
    return {name: _complete_file_identity(info) for name, info in present.items()}


def _observe_present_snapshot(
    snapshot_path: Path,
    snapshot_fd: int,
    snapshot_identity: Tuple[int, int, int, int, int],
    plan_snapshot_id: str,
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
            label="target snapshot",
        )
        first_present = _observe_listing(snapshot_path, snapshot_fd, opened_identity)
    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error
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
        _after_first_snapshot_observation(snapshot_path, first_records)
        try:
            second_dir = _observe_held_directory(
                snapshot_path,
                snapshot_fd,
                snapshot_identity,
                label="target snapshot",
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
            raise SnapshotImportReconcileIntegrityError(
                "target snapshot listing or payload set changed during reconciliation"
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
                label="target snapshot",
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
            raise SnapshotImportReconcileIntegrityError(
                "target snapshot listing, manifest, or payload changed "
                "during reconciliation"
            )
        try:
            if MANIFEST_NAME not in payload_fds:
                raise SnapshotImportReconcileIntegrityError(
                    "target snapshot is missing required payload manifest.json"
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
            raise SnapshotImportReconcileIntegrityError(
                "target snapshot manifest id differs from the saved plan snapshot id"
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


def _reobserve_present_snapshot(
    snapshot_path: Path,
    snapshot_fd: int,
    snapshot_identity: Tuple[int, int, int, int, int],
    expected_listing: Mapping[str, Tuple[int, int, int, int, int, int]],
    payload_fds: Mapping[str, int],
    identities: Mapping[str, Tuple[int, int, int, int, int, int]],
    revisions: Mapping[str, str],
) -> None:
    """Recheck held payload bytes after the second target-state scan."""
    opened_identity = _directory_identity(os.fstat(snapshot_fd))
    try:
        before_dir = _observe_held_directory(
            snapshot_path,
            snapshot_fd,
            snapshot_identity,
            label="target snapshot",
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
        raise SnapshotImportReconcileIntegrityError(
            "target snapshot listing or payload set changed before the final recheck"
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
            label="target snapshot",
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
        raise SnapshotImportReconcileIntegrityError(
            "target snapshot listing or payload changed during the final recheck"
        )


def _build_result(
    *,
    plan: Mapping[str, Any],
    plan_file: Path,
    apply_result_file: Optional[Path],
    apply_supplied: bool,
    apply_valid: bool,
    declared_outcome: str,
    canonical_graph: Path,
    current: str,
    published: Sequence[str],
    snapshot_state: str,
    observed_revision: Optional[str],
    snapshot_matches_plan: bool,
    staging_present: bool,
) -> Dict[str, Any]:
    snapshot_id = str(plan["snapshot_id"])
    planned = [dict(item) for item in plan["files"]]
    published_list = list(published)
    plan_published = set(plan["published_snapshots"])
    history_matches = set(published_list) == (plan_published | {snapshot_id})
    return {
        "schema_version": RECONCILE_SCHEMA_VERSION,
        "ok": True,
        "graph": str(canonical_graph),
        "plan_file": str(plan_file),
        "apply_result_file": None if apply_result_file is None else str(apply_result_file),
        "input_import_revision": plan["import_revision"],
        "source_export_revision": plan["source_export_revision"],
        "snapshot_id": snapshot_id,
        "planned_files": planned,
        "file_count": plan["file_count"],
        "total_size_bytes": plan["total_size_bytes"],
        "apply_result_supplied": apply_supplied,
        "apply_result_valid": apply_valid,
        "declared_apply_outcome": declared_outcome,
        "current": current,
        "current_matches_plan": current == plan["current"],
        "snapshot_active": current == snapshot_id,
        "published_snapshots": published_list,
        "published_count": len(published_list),
        "published_snapshot_present": snapshot_state != _STATE_ABSENT,
        "published_snapshot_state": snapshot_state,
        "observed_snapshot_export_revision": observed_revision,
        "snapshot_matches_plan": snapshot_matches_plan,
        "target_staging_name": plan["target_staging_name"],
        "target_staging_present": staging_present,
        "published_history_matches_plan_plus_target": history_matches,
        "creation_cause_proven": False,
        "recovery_performed": False,
        "graph_mutated": False,
        "export_observed": False,
        "export_mutated": False,
        "activation_performed": False,
        "retention_performed": False,
        "fresh_plan_required_before_import": True,
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
def _snapshot_import_reconcile_scope(
    graph: Path,
    plan_file: Path,
    apply_result_file: Optional[Path] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield one reconcile result while the graph lease and descriptors stay held."""
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
        raise SnapshotImportReconcileError(str(error)) from error
    graph_path, graph_path_identity = _resolve_graph_root(graph)
    _require_managed_graph(graph_path)
    lock_path = graph_path / PUBLICATION_LOCK_NAME
    try:
        lock_info = lock_path.lstat()
    except FileNotFoundError as error:
        raise SnapshotImportReconcileError(
            f"publication lock is missing; refusing to reconcile "
            f"an unleased managed graph: {lock_path}\n{_MISSING_LOCK_HINT}"
        ) from error
    except OSError as error:
        raise SnapshotImportReconcileError(
            f"cannot inspect publication lock {lock_path}: {error}"
        ) from error
    if stat.S_ISLNK(lock_info.st_mode) or not stat.S_ISREG(lock_info.st_mode):
        raise SnapshotImportReconcileIntegrityError(
            f"publication lock is not a regular file: {lock_path}"
        )
    _after_graph_path_inspected(graph_path)
    graph_fd: Optional[int] = None
    snapshots_fd: Optional[int] = None
    snapshot_fd: Optional[int] = None
    payload_fds: Dict[str, int] = {}
    try:
        _require_path_identity(graph_path, graph_path_identity, label="graph root")
        with graph_read_lease(graph_path, allow_unlocked_managed=False):
            canonical_graph, graph_fd, graph_identity = _open_anchored_graph(
                graph_path, graph_path_identity
            )
            if plan["graph"] != str(canonical_graph):
                raise SnapshotImportReconcileIntegrityError(
                    f"saved plan graph {plan['graph']!r} does not match requested "
                    f"graph {str(canonical_graph)!r}"
                )
            snapshots_path, snapshots_fd, snapshots_identity = _open_anchored_snapshots(
                canonical_graph, graph_fd, graph_identity
            )
            snapshot_id = str(plan["snapshot_id"])
            staging_name = str(plan["target_staging_name"])
            snapshot_path = snapshots_path / snapshot_id
            staging_path = snapshots_path / staging_name
            first = _scan_target(
                canonical_graph,
                graph_fd,
                graph_identity,
                snapshots_path,
                snapshots_fd,
                snapshots_identity,
                snapshot_id,
                staging_name,
            )
            _after_first_target_scan(canonical_graph, first)
            first_snapshot = _stat_child(
                snapshots_fd, snapshot_id, snapshot_path, label="target snapshot"
            )
            _after_target_snapshot_first_stat(snapshot_path, first_snapshot)
            first_staging = _stat_child(
                snapshots_fd,
                staging_name,
                staging_path,
                label="import staging directory",
            )
            _after_target_staging_first_stat(staging_path, first_staging)
            observed_revision: Optional[str] = None
            snapshot_state = _STATE_ABSENT
            snapshot_matches = False
            expected_listing: Dict[
                str, Tuple[int, int, int, int, int, int]
            ] = {}
            payload_identities: Dict[
                str, Tuple[int, int, int, int, int, int]
            ] = {}
            payload_revisions: Dict[str, str] = {}
            if first_snapshot is not None:
                snapshot_fd, snapshot_identity = _open_child_directory(
                    snapshots_fd,
                    snapshot_id,
                    snapshot_path,
                    first_snapshot,
                    label="target snapshot",
                )
                _after_target_snapshot_opened(
                    snapshot_path, snapshot_fd, snapshot_identity
                )
                (
                    _records,
                    observed_revision,
                    payload_fds,
                    expected_listing,
                    payload_identities,
                    payload_revisions,
                ) = _observe_present_snapshot(
                    snapshot_path, snapshot_fd, snapshot_identity, snapshot_id
                )
                if observed_revision == plan["source_export_revision"]:
                    snapshot_state = _STATE_MATCHES
                    snapshot_matches = True
                else:
                    snapshot_state = _STATE_MISMATCH
                    snapshot_matches = False
            second = _scan_target(
                canonical_graph,
                graph_fd,
                graph_identity,
                snapshots_path,
                snapshots_fd,
                snapshots_identity,
                snapshot_id,
                staging_name,
            )
            if _comparable_scan(first) != _comparable_scan(second):
                raise SnapshotImportReconcileIntegrityError(
                    "publication lock, current, snapshots listing, target "
                    "snapshot, or exact staging changed during reconciliation"
                )
            _after_second_target_scan(canonical_graph, second)
            if first_snapshot is not None:
                assert snapshot_fd is not None
                _reobserve_present_snapshot(
                    snapshot_path,
                    snapshot_fd,
                    snapshot_identity,
                    expected_listing,
                    payload_fds,
                    payload_identities,
                    payload_revisions,
                )
            third = _scan_target(
                canonical_graph,
                graph_fd,
                graph_identity,
                snapshots_path,
                snapshots_fd,
                snapshots_identity,
                snapshot_id,
                staging_name,
            )
            if _comparable_scan(second) != _comparable_scan(third):
                raise SnapshotImportReconcileIntegrityError(
                    "publication lock, current, snapshots listing, target "
                    "snapshot, or exact staging changed during the final "
                    "payload recheck"
                )
            final_snapshot = _stat_child(
                snapshots_fd, snapshot_id, snapshot_path, label="target snapshot"
            )
            final_staging = _stat_child(
                snapshots_fd,
                staging_name,
                staging_path,
                label="import staging directory",
            )
            if (first_snapshot is None) != (final_snapshot is None):
                raise SnapshotImportReconcileIntegrityError(
                    f"target snapshot changed during reconciliation: {snapshot_path}"
                )
            if first_snapshot is None and final_snapshot is None:
                snapshot_state = _STATE_ABSENT
                observed_revision = None
                snapshot_matches = False
            else:
                assert first_snapshot is not None and final_snapshot is not None
                if snapshot_fd is None:
                    raise SnapshotImportReconcileIntegrityError(
                        f"target snapshot changed during reconciliation: {snapshot_path}"
                    )
                held = os.fstat(snapshot_fd)
                if (
                    stat.S_ISLNK(final_snapshot.st_mode)
                    or not stat.S_ISDIR(final_snapshot.st_mode)
                    or _child_inode(final_snapshot) != _child_inode(held)
                    or _child_inode(first_snapshot) != _child_inode(held)
                ):
                    raise SnapshotImportReconcileIntegrityError(
                        f"target snapshot changed or was replaced: {snapshot_path}"
                    )
            if (first_staging is None) != (final_staging is None):
                raise SnapshotImportReconcileIntegrityError(
                    f"exact import staging changed during reconciliation: {staging_path}"
                )
            if first_staging is not None and final_staging is not None:
                if _child_inode(first_staging) != _child_inode(final_staging):
                    raise SnapshotImportReconcileIntegrityError(
                        f"exact import staging changed or was replaced: {staging_path}"
                    )
            result = _build_result(
                plan=plan,
                plan_file=plan_path,
                apply_result_file=apply_path,
                apply_supplied=apply_supplied,
                apply_valid=apply_valid,
                declared_outcome=declared_outcome,
                canonical_graph=canonical_graph,
                current=str(third["current"]),
                published=list(third["published"]),
                snapshot_state=snapshot_state,
                observed_revision=observed_revision,
                snapshot_matches_plan=snapshot_matches,
                staging_present=final_staging is not None,
            )
            _after_result_ready(
                canonical_graph,
                graph_fd,
                snapshots_fd,
                snapshot_fd,
                payload_fds,
                result,
            )
            yield result
    except SnapshotImportPlanError as error:
        raise _wrap_plan_error(error) from error
    except SnapshotStagingError as error:
        raise _wrap_plan_error(error) from error
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error
    finally:
        for fd in payload_fds.values():
            _close_fd(fd)
        _close_fd(snapshot_fd)
        _close_fd(snapshots_fd)
        _close_fd(graph_fd)


def snapshot_import_reconcile(
    graph: Path,
    plan_file: Path,
    apply_result_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reconcile one saved import plan against a managed graph without writing."""
    with _snapshot_import_reconcile_scope(
        graph, plan_file, apply_result_file
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile a saved snapshot-import-plan and optional saved "
            "snapshot-import-apply result against one managed BYOG graph. "
            "Observation only. Does not retry, recover, copy, publish, "
            "activate, pin, prune, clean staging, run retention, or mutate "
            "the graph or the standalone export. Never creates "
            ".publish.lock, and is not an MCP tool. A fresh import plan is "
            "required before any later apply."
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
        help="Saved schema-1 snapshot-import-plan JSON, relative to cwd.",
    )
    parser.add_argument(
        "--apply-result-file",
        type=Path,
        default=None,
        help="Optional saved schema-1 snapshot-import-apply JSON.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_import_reconcile_scope(
            args.graph, args.plan_file, args.apply_result_file
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            sys.stdout.flush()
            return 0 if result["ok"] else 1
    except SnapshotImportReconcileError as error:
        print(f"snapshot-import-reconcile: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-import-reconcile: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
