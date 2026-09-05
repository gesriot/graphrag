#!/usr/bin/env python
"""Read-only standalone snapshot export reconciliation.

``snapshot-export-reconcile`` inspects the destination after a complete,
partial, interrupted, or externally modified export attempt. It is
observation-only. It does not recover, retry, copy, repair, rename,
quarantine, delete, import, restore, or mutate anything. It does not
claim backup, authenticity, recoverability, provenance, or that
``snapshot-export-apply`` created or removed the observed path.

The command requires a saved schema-1 ``snapshot-export-plan`` file and
an exact standalone ``--destination``. An optional saved schema-1
``snapshot-export-apply`` result may be supplied for conservative
comparison. Both input paths are relative to the invoking cwd unless
absolute. Only bounded regular files are accepted; they are opened
read-only without following symlinks. The conservative limit is
``MAX_INPUT_BYTES`` (1 MiB).

Input loading and complete plan/result validation finish before the
destination is inspected. Malformed, oversized, symlinked, replaced,
truncated, or structurally invalid inputs fail with exit 2 and empty
stdout. A structurally valid apply result that refers to another plan
or destination is an integrity failure: exit 1 and empty stdout.

The command does not inspect a managed graph, read ``current``,
``snapshots/``, pins, staging, or ``.publish.lock``, or acquire a
graph lease. Destination observation reuses the snapshot-export
verification hashing/listing contract through the same plan helpers
without invoking a public CLI or creating a nested observation window.
A fresh export plan is still required before any later apply. MCP stays
exactly 17 read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-export-reconcile --plan-file <saved-plan.json> \\
        --destination <path> [--apply-result-file <saved-apply-result.json>] [--json]
    python -m graphrag_code.snapshot_export_reconcile --plan-file <saved-plan.json> \\
        --destination <path> [--apply-result-file <saved-apply-result.json>] [--json]
    uv run python scripts/snapshot_export_reconcile.py --plan-file <saved-plan.json> \\
        --destination <path> [--apply-result-file <saved-apply-result.json>] [--json]
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

from graphrag_code.byog_graph import is_published_snapshot_id
from graphrag_code.byog_snapshot_integrity import (
    MANIFEST_NAME,
    OBS_PARQUET,
    REQUIRED_PARQUETS,
    SETTINGS_NAME,
)
from graphrag_code.snapshot_export_plan import (
    ACCEPTED_PAYLOAD_FILES,
    PLAN_SCHEMA_VERSION,
    REQUIRED_PAYLOAD_FILES,
    SnapshotExportPlanError,
    SnapshotExportPlanIntegrityError,
    _directory_identity,
    _file_identity,
    _is_canonical_direct_name,
    _load_manifest,
    _payload_children,
    _planned_payload_names,
    _require_descriptor_reads,
    _stream_regular_file,
    canonical_export_revision_payload,
    export_revision_of,
)
from graphrag_code.snapshot_read import CURRENT_REF

RECONCILE_SCHEMA_VERSION = 1
APPLY_SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1 * 1024 * 1024
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_STATE_ABSENT = "absent"
_STATE_MATCHES = "matches_plan"
_STATE_MISMATCH = "revision_mismatch"
_DECLARED_NOT_SUPPLIED = "not_supplied"
_DECLARED_COMPLETE = "complete"
_DECLARED_PARTIAL = "partial"
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "reconciliation_is_observation_only",
        "kind": "notice",
        "message": (
            "snapshot-export-reconcile is observation-only. ok means this "
            "read completed, not that snapshot-export-apply succeeded or "
            "that the destination matches the saved plan."
        ),
    },
    {
        "code": "absence_does_not_prove_apply_failed",
        "kind": "notice",
        "message": (
            "An absent destination does not prove snapshot-export-apply "
            "failed or that another actor deleted the destination."
        ),
    },
    {
        "code": "presence_does_not_prove_apply_created",
        "kind": "notice",
        "message": (
            "A present destination does not prove snapshot-export-apply "
            "created it. creation_cause_proven is always false."
        ),
    },
    {
        "code": "revision_equality_is_observation_window_only",
        "kind": "notice",
        "message": (
            "Revision equality proves only equality with the saved plan's "
            "canonical payload contract during the observation window."
        ),
    },
    {
        "code": "fresh_plan_required_before_export",
        "kind": "notice",
        "message": (
            "A fresh snapshot-export-plan is still required before any "
            "later apply. This reconciliation is not a retry token."
        ),
    },
    {
        "code": "no_recovery_performed",
        "kind": "notice",
        "message": (
            "recovery_performed is always false. This command does not "
            "recover, retry, copy, repair, rename, quarantine, delete, "
            "import, restore, or mutate anything, and it is not "
            "authorization to delete anything."
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
            "snapshot-export-reconcile is CLI-only and intentionally "
            "absent from the fixed 14-tool MCP set."
        ),
    },
)


class SnapshotExportReconcileError(Exception):
    """Malformed arguments, inputs, or unsupported invocation. Default exit 2."""

    exit_code = 2


class SnapshotExportReconcileIntegrityError(SnapshotExportReconcileError):
    """Unsafe structure, invalid envelope, result mismatch, or dest change. Exit 1."""

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
    observed = result.get("observed_export_revision")
    if observed is None:
        observed_text = "null"
    else:
        observed_text = str(observed)
    return (
        "snapshot-export-reconcile: "
        f"destination={result.get('destination')} "
        f"destination_state={result.get('destination_state')} "
        f"resolved={result.get('resolved_snapshot')} "
        f"input_plan_revision={result.get('input_plan_revision')} "
        f"observed_export_revision={observed_text} "
        "destination_matches_plan="
        f"{str(bool(result.get('destination_matches_plan'))).lower()} "
        f"files={result.get('file_count')} "
        f"total_size_bytes={result.get('total_size_bytes')} "
        f"declared_apply_outcome={result.get('declared_apply_outcome')} "
        f"ok={str(bool(result.get('ok'))).lower()} "
        "This reconciliation is observation-only and is not authorization "
        "to delete anything."
    )


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def _after_input_path_lstat(_path: Path) -> None:
    """Test hook after lstat and before the no-follow input open."""
    return None


def _after_destination_path_inspected(_destination: Path) -> None:
    """Test hook after destination-parent inspection and before parent open."""
    return None


def _after_destination_parent_opened(
    _parent: Path, _parent_fd: int, _destination: Path
) -> None:
    """Test hook after the destination parent is anchored."""
    return None


def _after_destination_child_first_stat(
    _destination: Path, _info: Optional[os.stat_result]
) -> None:
    """Test hook after the first descriptor-relative destination child stat."""
    return None


def _after_destination_opened(
    _destination: Path,
    _directory_fd: int,
    _identity: Tuple[int, int, int, int],
) -> None:
    """Test hook after the destination directory is anchored."""
    return None


def _after_first_observation(
    _destination: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after the first complete destination observation."""
    return None


def _after_second_listed(
    _destination: Path, _entries: Mapping[str, os.stat_result]
) -> None:
    """Test hook after the second-pass listing and before the second hashes."""
    return None


def _after_result_ready(
    _destination: Path,
    _parent_fd: int,
    _directory_fd: Optional[int],
    _result: Mapping[str, Any],
) -> None:
    """Test hook after the result is built and before it is yielded."""
    return None


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant {value!r}")


def _require_revision(value: object, label: str) -> str:
    if not isinstance(value, str) or not _REVISION_RE.fullmatch(value):
        raise SnapshotExportReconcileError(
            f"{label} must be sha256:<64 lowercase hex>, got {value!r}"
        )
    return value


def _require_bool(value: object, label: str, *, expected: Optional[bool] = None) -> bool:
    if not isinstance(value, bool):
        raise SnapshotExportReconcileError(f"{label} must be a boolean")
    if expected is not None and value is not expected:
        raise SnapshotExportReconcileError(f"{label} must be {str(expected).lower()}")
    return value


def _require_non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotExportReconcileError(f"{label} must be a non-empty string")
    return value


def _require_absolute_string(value: object, label: str) -> str:
    text = _require_non_empty_string(value, label)
    if not Path(text).is_absolute():
        raise SnapshotExportReconcileError(f"{label} must be an absolute path")
    return text


def _read_bounded_regular_file(path: Path, *, label: str) -> bytes:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    try:
        before = resolved.lstat()
    except FileNotFoundError as error:
        raise SnapshotExportReconcileError(
            f"{label} does not exist: {resolved}"
        ) from error
    except OSError as error:
        raise SnapshotExportReconcileError(
            f"cannot inspect {label} {resolved}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotExportReconcileError(
            f"{label} must be a regular file, not a symlink: {resolved}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotExportReconcileError(
            f"{label} is not a regular file: {resolved}"
        )
    if before.st_size > MAX_INPUT_BYTES:
        raise SnapshotExportReconcileError(
            f"{label} exceeds bound {MAX_INPUT_BYTES} bytes: {resolved}"
        )
    if not hasattr(os, "O_NOFOLLOW"):
        raise SnapshotExportReconcileError(
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
            raise SnapshotExportReconcileError(
                f"{label} changed or became unsafe while opening it: {resolved}"
            ) from error
        raise SnapshotExportReconcileError(
            f"cannot safely open {label} {resolved}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = resolved.lstat()
        except OSError as error:
            raise SnapshotExportReconcileError(
                f"{label} changed while opening it: {resolved}"
            ) from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise SnapshotExportReconcileError(
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
            raise SnapshotExportReconcileError(
                f"{label} exceeds bound {MAX_INPUT_BYTES} bytes: {resolved}"
            )
        after_fd = os.fstat(fd)
        try:
            after_path = resolved.lstat()
        except OSError as error:
            raise SnapshotExportReconcileError(
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
            raise SnapshotExportReconcileError(
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
        raise SnapshotExportReconcileError(
            f"{label} is not valid UTF-8: {path}"
        ) from error
    try:
        parsed = json.loads(text, parse_constant=_reject_nonstandard_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise SnapshotExportReconcileError(
            f"{label} is not valid JSON: {error}"
        ) from error
    if not isinstance(parsed, dict):
        raise SnapshotExportReconcileError(f"{label} must be a JSON object")
    return parsed


def _file_records(value: object, *, label: str, resolved: str) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        raise SnapshotExportReconcileError(f"{label} must be an array")
    try:
        canonical = canonical_export_revision_payload(
            {
                "schema_version": PLAN_SCHEMA_VERSION,
                "resolved_snapshot": resolved,
                "files": value,
            }
        )
    except SnapshotExportPlanError as error:
        raise SnapshotExportReconcileError(f"{label}: {error}") from error
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
        raise SnapshotExportReconcileError(
            f"{label} is missing required envelope payload {missing[0]}"
        )
    extra = [name for name in names if name not in ACCEPTED_PAYLOAD_FILES]
    if extra:
        raise SnapshotExportReconcileError(
            f"{label} contains an unexpected envelope name: {extra[0]!r}"
        )
    return records


def _validate_saved_plan(plan: Mapping[str, Any]) -> Dict[str, Any]:
    schema = plan.get("schema_version")
    if isinstance(schema, bool) or schema != PLAN_SCHEMA_VERSION:
        raise SnapshotExportReconcileError("saved plan schema_version must be 1")
    _require_bool(plan.get("ok"), "saved plan ok", expected=True)
    _require_bool(
        plan.get("export_performed"),
        "saved plan export_performed",
        expected=False,
    )
    _require_bool(
        plan.get("fresh_plan_required_before_export"),
        "saved plan fresh_plan_required_before_export",
        expected=True,
    )
    graph = _require_absolute_string(plan.get("graph"), "saved plan graph")
    snapshot_path = _require_absolute_string(
        plan.get("snapshot_path"), "saved plan snapshot_path"
    )
    requested = _require_non_empty_string(
        plan.get("requested_snapshot"), "saved plan requested_snapshot"
    )
    resolved = _require_non_empty_string(
        plan.get("resolved_snapshot"), "saved plan resolved_snapshot"
    )
    if requested != CURRENT_REF:
        if (
            not _is_canonical_direct_name(requested)
            or not is_published_snapshot_id(requested)
        ):
            raise SnapshotExportReconcileError(
                "saved plan requested_snapshot must be current or a "
                "canonical published snapshot id"
            )
        if requested != resolved:
            raise SnapshotExportReconcileError(
                "saved plan requested_snapshot does not match resolved_snapshot"
            )
    if not _is_canonical_direct_name(resolved) or not is_published_snapshot_id(resolved):
        raise SnapshotExportReconcileError(
            "saved plan resolved_snapshot is not a canonical published snapshot id"
        )
    records = _file_records(
        plan.get("files"), label="saved plan files", resolved=resolved
    )
    file_count = plan.get("file_count")
    if isinstance(file_count, bool) or file_count != len(records):
        raise SnapshotExportReconcileError(
            "saved plan file_count must equal the number of file records"
        )
    total = plan.get("total_size_bytes")
    expected_total = sum(int(item["size_bytes"]) for item in records)
    if isinstance(total, bool) or total != expected_total:
        raise SnapshotExportReconcileError(
            "saved plan total_size_bytes must equal the sum of file sizes"
        )
    notices = plan.get("notices")
    if not isinstance(notices, list):
        raise SnapshotExportReconcileError("saved plan notices must be an array")
    declared = _require_revision(plan.get("export_revision"), "saved plan export_revision")
    try:
        recomputed = export_revision_of(
            {
                "schema_version": PLAN_SCHEMA_VERSION,
                "resolved_snapshot": resolved,
                "files": records,
            }
        )
    except SnapshotExportPlanError as error:
        raise SnapshotExportReconcileError(str(error)) from error
    if recomputed != declared:
        raise SnapshotExportReconcileError(
            "saved plan export_revision does not match the canonical "
            "snapshot-export-plan contract"
        )
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "ok": True,
        "graph": graph,
        "requested_snapshot": requested,
        "resolved_snapshot": resolved,
        "snapshot_path": snapshot_path,
        "files": records,
        "file_count": len(records),
        "total_size_bytes": expected_total,
        "export_performed": False,
        "fresh_plan_required_before_export": True,
        "export_revision": declared,
        "notices": notices,
    }


def _validate_apply_outcome(result: Mapping[str, Any]) -> str:
    ok = _require_bool(result.get("ok"), "saved apply result ok")
    partial = _require_bool(result.get("partial"), "saved apply result partial")
    export_confirmed = _require_bool(
        result.get("export_confirmed"), "saved apply result export_confirmed"
    )
    export_performed = _require_bool(
        result.get("export_performed"), "saved apply result export_performed"
    )
    destination_created = _require_bool(
        result.get("destination_created"), "saved apply result destination_created"
    )
    destination_verified = _require_bool(
        result.get("destination_verified"), "saved apply result destination_verified"
    )
    source_unchanged = _require_bool(
        result.get("source_unchanged"), "saved apply result source_unchanged"
    )
    parent_fsync_confirmed = _require_bool(
        result.get("parent_fsync_confirmed"),
        "saved apply result parent_fsync_confirmed",
    )
    error = result.get("error")
    if error is not None and (not isinstance(error, str) or error == ""):
        raise SnapshotExportReconcileError(
            "saved apply result error must be a non-empty string or null"
        )
    complete = (
        ok is True
        and partial is False
        and export_confirmed is True
        and export_performed is True
        and destination_created is True
        and destination_verified is True
        and source_unchanged is True
        and parent_fsync_confirmed is True
        and error is None
    )
    post_publication_partial = (
        ok is False
        and partial is True
        and export_confirmed is True
        and export_performed is True
        and destination_created is True
        and source_unchanged is True
        and isinstance(error, str)
        and error != ""
        and not (destination_verified is True and parent_fsync_confirmed is True)
    )
    if complete:
        return _DECLARED_COMPLETE
    if post_publication_partial:
        return _DECLARED_PARTIAL
    raise SnapshotExportReconcileError(
        "saved apply result flags are not an exact complete-success or "
        "emitted post-publication partial outcome"
    )


def _validate_saved_apply_result(
    result: Mapping[str, Any], plan: Mapping[str, Any]
) -> Tuple[str, Dict[str, Any]]:
    schema = result.get("schema_version")
    if isinstance(schema, bool) or schema != APPLY_SCHEMA_VERSION:
        raise SnapshotExportReconcileError(
            "saved apply result schema_version must be 1"
        )
    outcome = _validate_apply_outcome(result)
    graph = _require_absolute_string(result.get("graph"), "saved apply result graph")
    requested = _require_non_empty_string(
        result.get("requested_snapshot"), "saved apply result requested_snapshot"
    )
    resolved = _require_non_empty_string(
        result.get("resolved_snapshot"), "saved apply result resolved_snapshot"
    )
    destination = _require_absolute_string(
        result.get("destination"), "saved apply result destination"
    )
    records = _file_records(
        result.get("files"), label="saved apply result files", resolved=resolved
    )
    file_count = result.get("file_count")
    if isinstance(file_count, bool) or file_count != len(records):
        raise SnapshotExportReconcileError(
            "saved apply result file_count must equal the number of file records"
        )
    total = result.get("total_size_bytes")
    expected_total = sum(int(item["size_bytes"]) for item in records)
    if isinstance(total, bool) or total != expected_total:
        raise SnapshotExportReconcileError(
            "saved apply result total_size_bytes must equal the sum of file sizes"
        )
    expected = _require_revision(
        result.get("expected_export_revision"),
        "saved apply result expected_export_revision",
    )
    observed = _require_revision(
        result.get("observed_export_revision"),
        "saved apply result observed_export_revision",
    )
    notices = result.get("notices")
    if not isinstance(notices, list):
        raise SnapshotExportReconcileError(
            "saved apply result notices must be an array"
        )
    if (
        graph != plan["graph"]
        or requested != plan["requested_snapshot"]
        or resolved != plan["resolved_snapshot"]
        or records != plan["files"]
        or file_count != plan["file_count"]
        or expected_total != plan["total_size_bytes"]
        or expected != plan["export_revision"]
        or observed != plan["export_revision"]
    ):
        raise SnapshotExportReconcileIntegrityError(
            "saved apply result refers to another plan"
        )
    return outcome, {
        "schema_version": APPLY_SCHEMA_VERSION,
        "destination": destination,
        "files": records,
        "expected_export_revision": expected,
        "observed_export_revision": observed,
        "declared_apply_outcome": outcome,
        "notices": notices,
    }


def _destination_parts(destination: object) -> Tuple[Path, Path, str]:
    if destination is None or (isinstance(destination, str) and destination == ""):
        raise SnapshotExportReconcileError("destination is required")
    path = Path(destination)
    if not path.is_absolute():
        path = Path.cwd() / path
    dest_name = path.name
    if not _is_canonical_direct_name(dest_name):
        raise SnapshotExportReconcileError(
            f"destination name is not a canonical direct name: {dest_name!r}"
        )
    return path.parent, path, dest_name


def _open_destination_parent(
    parent: Path, dest_path: Path
) -> Tuple[int, Tuple[int, int, int, int]]:
    try:
        before = parent.lstat()
    except FileNotFoundError as error:
        raise SnapshotExportReconcileError(
            f"destination parent does not exist: {parent}"
        ) from error
    except OSError as error:
        raise SnapshotExportReconcileError(
            f"cannot inspect destination parent {parent}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotExportReconcileError(
            f"destination parent must be a real directory, not a symlink: {parent}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotExportReconcileError(
            f"destination parent is not a real directory: {parent}"
        )
    _after_destination_path_inspected(dest_path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(parent), flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise SnapshotExportReconcileIntegrityError(
                f"destination parent changed or became unsafe while opening it: "
                f"{parent}"
            ) from error
        raise SnapshotExportReconcileError(
            f"cannot safely open destination parent {parent}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = parent.lstat()
        except OSError as error:
            raise SnapshotExportReconcileIntegrityError(
                f"destination parent changed while opening it: {parent}"
            ) from error
        opened_identity = _directory_identity(opened)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or opened_identity != _directory_identity(before)
            or _directory_identity(current) != opened_identity
        ):
            raise SnapshotExportReconcileIntegrityError(
                f"destination parent changed or became unsafe while opening it: "
                f"{parent}"
            )
    except Exception:
        os.close(fd)
        raise
    return fd, opened_identity


def _canonical_parent(
    parent: Path, parent_fd: int, expected_identity: Tuple[int, int, int, int]
) -> Path:
    try:
        resolved = parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SnapshotExportReconcileIntegrityError(
            f"destination parent changed during canonicalization: {parent}"
        ) from error
    try:
        resolved_info = resolved.lstat()
        held = os.fstat(parent_fd)
    except OSError as error:
        raise SnapshotExportReconcileIntegrityError(
            f"destination parent changed during canonicalization: {parent}"
        ) from error
    if (
        stat.S_ISLNK(resolved_info.st_mode)
        or not stat.S_ISDIR(resolved_info.st_mode)
        or not stat.S_ISDIR(held.st_mode)
        or _directory_identity(held) != expected_identity
        or _directory_identity(resolved_info) != expected_identity
    ):
        raise SnapshotExportReconcileIntegrityError(
            f"destination parent changed or no longer names the held directory: "
            f"{parent}"
        )
    return resolved


def _require_parent_held(
    parent: Path, parent_fd: int, expected_identity: Tuple[int, int, int, int]
) -> None:
    try:
        held = os.fstat(parent_fd)
        current = parent.lstat()
    except OSError as error:
        raise SnapshotExportReconcileIntegrityError(
            f"destination parent changed during reconciliation: {parent}"
        ) from error
    if (
        not stat.S_ISDIR(held.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _directory_identity(held) != expected_identity
        or _directory_identity(current) != expected_identity
    ):
        raise SnapshotExportReconcileIntegrityError(
            f"destination parent changed or no longer names the held directory: "
            f"{parent}"
        )


def _stat_destination_child(
    parent_fd: int, dest_name: str, dest_path: Path
) -> Optional[os.stat_result]:
    try:
        return os.stat(dest_name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise SnapshotExportReconcileIntegrityError(
            f"cannot inspect destination {dest_path}: {error}"
        ) from error


def _open_destination_directory(
    parent_fd: int,
    dest_name: str,
    dest_path: Path,
    child_info: os.stat_result,
) -> Tuple[int, Tuple[int, int, int, int]]:
    if stat.S_ISLNK(child_info.st_mode):
        raise SnapshotExportReconcileIntegrityError(
            f"destination must be a real directory, not a symlink: {dest_path}"
        )
    if not stat.S_ISDIR(child_info.st_mode):
        raise SnapshotExportReconcileIntegrityError(
            f"destination is not a real directory: {dest_path}"
        )
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(dest_name, flags, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise SnapshotExportReconcileIntegrityError(
                f"destination changed or became unsafe while opening it: {dest_path}"
            ) from error
        raise SnapshotExportReconcileIntegrityError(
            f"cannot safely open destination {dest_path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = os.stat(dest_name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotExportReconcileIntegrityError(
                f"destination changed while opening it: {dest_path}"
            ) from error
        opened_identity = _directory_identity(opened)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or opened_identity != _directory_identity(child_info)
            or _directory_identity(current) != opened_identity
        ):
            raise SnapshotExportReconcileIntegrityError(
                f"destination changed or became unsafe while opening it: {dest_path}"
            )
    except Exception:
        os.close(fd)
        raise
    return fd, opened_identity


def _structure_error(error: Exception) -> SnapshotExportReconcileError:
    return SnapshotExportReconcileIntegrityError(str(error))


def _listing_token(
    present: Mapping[str, os.stat_result],
) -> Dict[str, Tuple[int, int, int, int, int]]:
    return {name: _file_identity(info) for name, info in present.items()}


def _observe_directory(
    dest_path: Path,
    directory_fd: int,
    expected_identity: Tuple[int, int, int, int],
) -> Tuple[int, int, int, int]:
    try:
        opened = os.fstat(directory_fd)
    except OSError as error:
        raise SnapshotExportReconcileIntegrityError(
            f"cannot inspect destination descriptor {dest_path}: {error}"
        ) from error
    opened_identity = _directory_identity(opened)
    if not stat.S_ISDIR(opened.st_mode) or opened_identity != expected_identity:
        raise SnapshotExportReconcileIntegrityError(
            "destination descriptor changed during reconciliation"
        )
    try:
        current = dest_path.lstat()
    except OSError as error:
        raise SnapshotExportReconcileIntegrityError(
            f"destination changed during reconciliation: {dest_path}"
        ) from error
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _directory_identity(current) != expected_identity
    ):
        raise SnapshotExportReconcileIntegrityError(
            f"destination changed or was replaced: {dest_path}"
        )
    return opened_identity


def _observe_listing(
    dest_path: Path,
    directory_fd: int,
    expected_identity: Tuple[int, int, int, int],
) -> Dict[str, os.stat_result]:
    try:
        return _payload_children(dest_path, directory_fd, expected_identity)
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _structure_error(error) from error


def _observe_payloads(
    dest_path: Path,
    directory_fd: int,
    present: Mapping[str, os.stat_result],
) -> Tuple[str, List[Dict[str, Any]], Dict[str, Tuple[int, int, int, int, int]], Dict[str, str]]:
    if MANIFEST_NAME not in present:
        raise SnapshotExportReconcileIntegrityError(
            "destination is missing required payload manifest.json"
        )
    try:
        manifest, manifest_revision, manifest_identity = _load_manifest(
            directory_fd, dest_path / MANIFEST_NAME
        )
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _structure_error(error) from error
    if manifest_identity != _file_identity(present[MANIFEST_NAME]):
        raise SnapshotExportReconcileIntegrityError(
            "manifest changed after the anchored payload listing"
        )
    resolved = manifest.get("id")
    if not isinstance(resolved, str):
        raise SnapshotExportReconcileIntegrityError(
            "manifest id is not a canonical published snapshot id"
        )
    try:
        planned = _planned_payload_names(manifest, present, resolved=resolved)
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _structure_error(error) from error
    records: List[Dict[str, Any]] = []
    identities: Dict[str, Tuple[int, int, int, int, int]] = {
        MANIFEST_NAME: manifest_identity
    }
    revisions: Dict[str, str] = {MANIFEST_NAME: manifest_revision}
    for name in planned:
        path = dest_path / name
        if name == MANIFEST_NAME:
            records.append(
                {
                    "path": name,
                    "size_bytes": manifest_identity[2],
                    "content_revision": manifest_revision,
                }
            )
            continue
        try:
            _data, revision, identity = _stream_regular_file(
                directory_fd, name, path, label=name
            )
        except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
            raise _structure_error(error) from error
        if identity != _file_identity(present[name]):
            raise SnapshotExportReconcileIntegrityError(
                f"payload {name} changed after the anchored listing"
            )
        identities[name] = identity
        revisions[name] = revision
        records.append(
            {
                "path": name,
                "size_bytes": identity[2],
                "content_revision": revision,
            }
        )
    return resolved, records, identities, revisions


def _verify_present_destination(
    dest_path: Path,
    directory_fd: int,
    opened_identity: Tuple[int, int, int, int],
) -> Tuple[str, List[Dict[str, Any]], str]:
    _observe_directory(dest_path, directory_fd, opened_identity)
    first_present = _observe_listing(dest_path, directory_fd, opened_identity)
    first_resolved, first_records, first_ids, first_revs = _observe_payloads(
        dest_path, directory_fd, first_present
    )
    _after_first_observation(dest_path, first_records)
    second_dir = _observe_directory(dest_path, directory_fd, opened_identity)
    second_present = _observe_listing(dest_path, directory_fd, opened_identity)
    _after_second_listed(dest_path, second_present)
    second_resolved, second_records, second_ids, second_revs = _observe_payloads(
        dest_path, directory_fd, second_present
    )
    final_dir = _observe_directory(dest_path, directory_fd, opened_identity)
    final_present = _observe_listing(dest_path, directory_fd, opened_identity)
    if (
        second_dir != opened_identity
        or final_dir != opened_identity
        or _listing_token(first_present) != _listing_token(second_present)
        or _listing_token(first_present) != _listing_token(final_present)
        or first_resolved != second_resolved
        or first_records != second_records
        or first_ids != second_ids
        or first_revs != second_revs
    ):
        raise SnapshotExportReconcileIntegrityError(
            "destination listing, manifest, or payload changed during reconciliation"
        )
    observed = export_revision_of(
        {
            "schema_version": PLAN_SCHEMA_VERSION,
            "resolved_snapshot": first_resolved,
            "files": first_records,
        }
    )
    return first_resolved, first_records, observed


def _build_result(
    *,
    plan: Mapping[str, Any],
    dest_resolved: Path,
    apply_supplied: bool,
    apply_valid: bool,
    declared_outcome: str,
    destination_state: str,
    destination_present: bool,
    destination_matches_plan: bool,
    resolved_snapshot: str,
    files: Sequence[Mapping[str, Any]],
    observed_export_revision: Optional[str],
) -> Dict[str, Any]:
    records = [dict(item) for item in files]
    return {
        "schema_version": RECONCILE_SCHEMA_VERSION,
        "ok": True,
        "input_plan_revision": plan["export_revision"],
        "input_plan_valid": True,
        "apply_result_supplied": apply_supplied,
        "apply_result_valid": apply_valid,
        "declared_apply_outcome": declared_outcome,
        "destination": str(dest_resolved),
        "destination_state": destination_state,
        "destination_present": destination_present,
        "destination_matches_plan": destination_matches_plan,
        "resolved_snapshot": resolved_snapshot,
        "files": records,
        "file_count": len(records),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in records),
        "observed_export_revision": observed_export_revision,
        "export_mutated": False,
        "graph_inspected": False,
        "recovery_performed": False,
        "creation_cause_proven": False,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }


@contextmanager
def _snapshot_export_reconcile_scope(
    plan_file: Path,
    destination: object,
    apply_result_file: Optional[Path] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield one reconcile result while parent/export descriptors stay held."""
    plan = _validate_saved_plan(_load_json_object(plan_file, label="plan file"))
    apply_supplied = apply_result_file is not None
    apply_valid = False
    declared_outcome = _DECLARED_NOT_SUPPLIED
    saved_apply: Optional[Dict[str, Any]] = None
    if apply_supplied:
        saved_apply_outcome, saved_apply = _validate_saved_apply_result(
            _load_json_object(apply_result_file, label="apply-result file"),
            plan,
        )
        apply_valid = True
        declared_outcome = saved_apply_outcome
    try:
        _require_descriptor_reads()
    except SnapshotExportPlanError as error:
        raise SnapshotExportReconcileError(str(error)) from error
    parent, dest_path, dest_name = _destination_parts(destination)
    parent_fd, parent_identity = _open_destination_parent(parent, dest_path)
    dest_fd: Optional[int] = None
    try:
        parent_resolved = _canonical_parent(parent, parent_fd, parent_identity)
        dest_resolved = parent_resolved / dest_name
        _after_destination_parent_opened(parent_resolved, parent_fd, dest_resolved)
        if saved_apply is not None and saved_apply["destination"] != str(dest_resolved):
            raise SnapshotExportReconcileIntegrityError(
                "saved apply result refers to another destination"
            )
        _require_parent_held(parent_resolved, parent_fd, parent_identity)
        first_child = _stat_destination_child(parent_fd, dest_name, dest_resolved)
        _after_destination_child_first_stat(dest_resolved, first_child)
        _require_parent_held(parent_resolved, parent_fd, parent_identity)
        second_child = _stat_destination_child(parent_fd, dest_name, dest_resolved)
        if first_child is None and second_child is None:
            result = _build_result(
                plan=plan,
                dest_resolved=dest_resolved,
                apply_supplied=apply_supplied,
                apply_valid=apply_valid,
                declared_outcome=declared_outcome,
                destination_state=_STATE_ABSENT,
                destination_present=False,
                destination_matches_plan=False,
                resolved_snapshot=str(plan["resolved_snapshot"]),
                files=[],
                observed_export_revision=None,
            )
            _after_result_ready(dest_resolved, parent_fd, None, result)
            yield result
            return
        if first_child is None or second_child is None:
            raise SnapshotExportReconcileIntegrityError(
                f"destination changed during reconciliation: {dest_resolved}"
            )
        if (first_child.st_dev, first_child.st_ino) != (
            second_child.st_dev,
            second_child.st_ino,
        ) or first_child.st_mode != second_child.st_mode:
            raise SnapshotExportReconcileIntegrityError(
                f"destination changed or was replaced: {dest_resolved}"
            )
        dest_fd, dest_identity = _open_destination_directory(
            parent_fd, dest_name, dest_resolved, second_child
        )
        _after_destination_opened(dest_resolved, dest_fd, dest_identity)
        resolved, records, observed = _verify_present_destination(
            dest_resolved, dest_fd, dest_identity
        )
        _require_parent_held(parent_resolved, parent_fd, parent_identity)
        final_child = _stat_destination_child(parent_fd, dest_name, dest_resolved)
        if final_child is None:
            raise SnapshotExportReconcileIntegrityError(
                f"destination disappeared during reconciliation: {dest_resolved}"
            )
        if stat.S_ISLNK(final_child.st_mode) or not stat.S_ISDIR(final_child.st_mode):
            raise SnapshotExportReconcileIntegrityError(
                f"destination changed or was replaced: {dest_resolved}"
            )
        if (final_child.st_dev, final_child.st_ino) != (
            dest_identity[0],
            dest_identity[1],
        ):
            raise SnapshotExportReconcileIntegrityError(
                f"destination changed or was replaced: {dest_resolved}"
            )
        _observe_directory(dest_resolved, dest_fd, dest_identity)
        matches = observed == plan["export_revision"]
        result = _build_result(
            plan=plan,
            dest_resolved=dest_resolved,
            apply_supplied=apply_supplied,
            apply_valid=apply_valid,
            declared_outcome=declared_outcome,
            destination_state=_STATE_MATCHES if matches else _STATE_MISMATCH,
            destination_present=True,
            destination_matches_plan=matches,
            resolved_snapshot=resolved,
            files=records,
            observed_export_revision=observed,
        )
        _after_result_ready(dest_resolved, parent_fd, dest_fd, result)
        yield result
    finally:
        if dest_fd is not None:
            os.close(dest_fd)
        os.close(parent_fd)


def snapshot_export_reconcile(
    plan_file: Path,
    destination: Path,
    apply_result_file: Optional[Path] = None,
) -> Dict[str, Any]:
    """Reconcile one standalone export destination without writing files."""
    with _snapshot_export_reconcile_scope(
        plan_file, destination, apply_result_file
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile a saved snapshot-export-plan and optional saved "
            "snapshot-export-apply result against one standalone destination. "
            "Observation only. Does not inspect a graph, mutate the "
            "destination, recover, or authorize deletion. Not an MCP tool."
        )
    )
    parser.add_argument(
        "--plan-file",
        type=Path,
        required=True,
        help="Saved schema-1 snapshot-export-plan JSON, relative to cwd.",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        required=True,
        help="Standalone destination to observe, relative to cwd.",
    )
    parser.add_argument(
        "--apply-result-file",
        type=Path,
        default=None,
        help="Optional saved schema-1 snapshot-export-apply JSON.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_export_reconcile_scope(
            args.plan_file, args.destination, args.apply_result_file
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            sys.stdout.flush()
            return 0 if result["ok"] else 1
    except SnapshotExportReconcileError as error:
        print(f"snapshot-export-reconcile: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-export-reconcile: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
