#!/usr/bin/env python
"""Read-only snapshot import plan.

``snapshot-import-plan`` inspects one standalone snapshot export directory
and one existing managed BYOG graph, then emits a deterministic schema-1
plan for adding that export as a published snapshot. It does not create a
snapshot or staging directory, copy, rename, link, replace, truncate,
chmod, unlink, or delete anything, change ``current``, or create or
modify ``.publish.lock``, ``.snapshot-pins.json``, or any writer lock.
It does not activate, pin, prune, clean staging, import, restore,
recover, or repair. ``import_performed`` is always false.
``graph_mutated`` and ``export_mutated`` are always false.
``fresh_plan_required_before_import`` is always true.

This command proves only the language-independent stored snapshot
envelope and observed bytes. It does not compare ``source_root``,
``git_commit``, or ``created_at`` with the current host, and it does
not run any language-specific or Clang overlay audit. The plan is not
a backup and is not a claim of authenticity, provenance, portability,
recoverability, or successful future import. A future import apply
attempt still requires a fresh plan. ``import_revision`` is a
self-consistency/CAS token accepted only by the explicit
``snapshot-import-apply`` command for that freshly reproduced plan.

The source export directory may be relative to the invoking cwd. It
must be an existing real directory, never a symlink. Payload reads are
descriptor-relative and no-follow, with bounded-memory streaming
hashes, a bounded manifest (1 MiB), and strict UTF-8/JSON. The export
directory and relevant payload descriptors stay open through target
observation, result construction, serialization, stdout write, and
flush.

The target graph may be relative to cwd. It must be an existing real
managed ``current + snapshots/`` graph with an already-existing regular
``.publish.lock``. The command never creates or adopts that lock.
Legacy-flat and unlocked managed graphs fail closed. One shared
existing-lock graph lease covers target observation, result
construction, serialization, stdout write, and flush. The command does
not acquire a nested graph lease.

MCP stays exactly 17 read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-import-plan --graph <root> --export-dir <directory> [--json]
    python -m graphrag_code.snapshot_import_plan --graph <root> --export-dir <directory> [--json]
    uv run python scripts/snapshot_import_plan.py --graph <root> --export-dir <directory> [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import io
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
    graph_read_lease,
    is_published_snapshot_id,
)
from graphrag_code.byog_snapshot_integrity import (
    MANIFEST_NAME,
    OBS_PARQUET,
    validate_persisted_byog_snapshot,
)
from graphrag_code.snapshot_export_plan import (
    HASH_CHUNK_BYTES,
    MAX_MANIFEST_BYTES,
    PLAN_SCHEMA_VERSION,
    SnapshotExportPlanError,
    SnapshotExportPlanIntegrityError,
    _is_canonical_direct_name,
    _open_directory_nofollow,
    _open_regular_nofollow,
    _payload_children,
    _planned_payload_names,
    _require_descriptor_reads,
    export_revision_of,
)
from graphrag_code.snapshot_staging import (
    SnapshotStagingError,
    SnapshotStagingIntegrityError,
    build_stable_staging_inventory_unlocked,
)

PLAN_SCHEMA_VERSION_IMPORT = 1
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BLOCK_PUBLISHED = "snapshot_id_already_published"
_BLOCK_STAGING = "target_staging_name_present"
_ALLOWED_BLOCKING = (_BLOCK_PUBLISHED, _BLOCK_STAGING)
_IMPORT_REVISION_KEYS = (
    "schema_version",
    "snapshot_id",
    "source_export_revision",
    "current",
    "published_snapshots",
    "target_staging_name",
    "target_snapshot_present",
    "target_staging_present",
    "blocking_reasons",
    "import_ready",
    "source_envelope_valid",
    "import_performed",
    "fresh_plan_required_before_import",
)
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "plan_is_not_import",
        "kind": "notice",
        "message": (
            "snapshot-import-plan is inspection only. import_performed is "
            "always false. It does not create a snapshot or staging "
            "directory, copy files, or mutate the graph or the export."
        ),
    },
    {
        "code": "plan_is_not_backup",
        "kind": "notice",
        "message": (
            "This plan is not a backup, archive, restore kit, or "
            "authorization to delete or import anything. It does not claim "
            "portability, recoverability, authenticity, or provenance."
        ),
    },
    {
        "code": "import_revision_is_self_consistency_only",
        "kind": "notice",
        "message": (
            "import_revision is a self-consistency/CAS-ready plan token for "
            "this exact observed source and target. It is accepted only by "
            "the explicit snapshot-import-apply command after that command "
            "freshly reproduces the same plan."
        ),
    },
    {
        "code": "fresh_plan_required_before_import",
        "kind": "notice",
        "message": (
            "fresh_plan_required_before_import is always true. "
            "snapshot-import-apply accepts only a freshly reproduced "
            "matching plan."
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
            "snapshot-import-plan is CLI-only and intentionally absent "
            "from the fixed 14-tool MCP set."
        ),
    },
)
_UNRELATED_STAGING_NOTICE = {
    "code": "unrelated_target_staging_present",
    "kind": "notice",
    "message": (
        "Other staging entries are reported through staging_count and "
        "do not independently block this source snapshot id."
    ),
}


class SnapshotImportPlanError(Exception):
    """Expected import-plan failure. Default exit 2."""

    exit_code = 2


class SnapshotImportPlanIntegrityError(SnapshotImportPlanError):
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
    ready = "true" if result.get("import_ready") is True else "false"
    return (
        "snapshot-import-plan: "
        f"graph={result.get('graph')} "
        f"export_directory={result.get('export_directory')} "
        f"snapshot_id={result.get('snapshot_id')} "
        f"current={result.get('current')} "
        f"published_count={result.get('published_count')} "
        f"staging_count={result.get('staging_count')} "
        f"import_ready={ready} "
        f"import_revision={result.get('import_revision')} "
        "import_performed=false "
        "fresh_plan_required_before_import=true "
        "This plan is not an import and is not a backup."
    )


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant {value!r}")


def _strict_json_loads(text: str) -> Any:
    def unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise ValueError(f"duplicate JSON object key {key!r}")
            out[key] = value
        return out

    return json.loads(
        text,
        parse_constant=_reject_nonstandard_json_constant,
        object_pairs_hook=unique_object,
    )


def _complete_file_identity(
    info: os.stat_result,
) -> Tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _complete_directory_identity(
    info: os.stat_result,
) -> Tuple[int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
    )


def _from_export_error(error: Exception) -> SnapshotImportPlanError:
    message = str(error)
    if isinstance(error, SnapshotExportPlanIntegrityError):
        return SnapshotImportPlanIntegrityError(message)
    if isinstance(error, SnapshotExportPlanError):
        lowered = message.lower()
        if (
            "exceeds bound" in lowered
            or "unsupported" in lowered
            or "not valid utf-8" in lowered
            or "not valid json" in lowered
            or "not a json object" in lowered
        ):
            wrapped = SnapshotImportPlanError(message)
            wrapped.exit_code = getattr(error, "exit_code", 2)
            return wrapped
        return SnapshotImportPlanIntegrityError(message)
    return SnapshotImportPlanError(message)


def _wrap_staging_error(error: Exception) -> SnapshotImportPlanError:
    if isinstance(error, SnapshotStagingIntegrityError):
        return SnapshotImportPlanIntegrityError(str(error))
    if isinstance(error, SnapshotStagingError):
        wrapped = SnapshotImportPlanError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    return SnapshotImportPlanError(str(error))


def _lock_error(error: Exception) -> SnapshotImportPlanError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotImportPlanError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotImportPlanIntegrityError(message)
    return SnapshotImportPlanError(message)


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotImportPlanError(str(error)) from error
    if not managed:
        raise SnapshotImportPlanError(
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
        raise SnapshotImportPlanError(f"{label} does not exist: {resolved}") from error
    except OSError as error:
        raise SnapshotImportPlanError(
            f"cannot inspect {label} {resolved}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotImportPlanIntegrityError(
            f"{label} must be a real directory, not a symlink: {resolved}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotImportPlanIntegrityError(
            f"{label} is not a real directory: {resolved}"
        )
    return resolved, _complete_directory_identity(before)


def _resolve_export_dir(
    export_dir: Path,
) -> Tuple[Path, Tuple[int, int, int, int, int]]:
    path, identity = _resolve_existing_real_directory(
        export_dir, label="export directory"
    )
    _after_import_source_path_inspected(path)
    # Canonicalization happens only after this pathname is opened and
    # anchored. Resolving here would follow a replacement symlink.
    return path, identity


def _resolve_graph_root(
    graph: Path,
) -> Tuple[Path, Tuple[int, int, int, int, int]]:
    path, identity = _resolve_existing_real_directory(graph, label="graph root")
    _after_import_graph_path_inspected(path)
    return path, identity


def _require_path_identity(
    path: Path,
    expected_identity: Tuple[int, int, int, int, int],
    *,
    label: str,
) -> None:
    try:
        current = path.lstat()
    except OSError as error:
        raise SnapshotImportPlanIntegrityError(
            f"{label} changed before it was anchored: {path}"
        ) from error
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _complete_directory_identity(current) != expected_identity
    ):
        raise SnapshotImportPlanIntegrityError(
            f"{label} changed or was replaced before it was anchored: {path}"
        )


def _observe_held_directory(
    path: Path,
    directory_fd: int,
    expected_identity: Tuple[int, int, int, int, int],
    *,
    label: str,
) -> Tuple[int, int, int, int, int]:
    try:
        opened = os.fstat(directory_fd)
    except OSError as error:
        raise SnapshotImportPlanIntegrityError(
            f"cannot inspect {label} descriptor {path}: {error}"
        ) from error
    opened_identity = _complete_directory_identity(opened)
    if not stat.S_ISDIR(opened.st_mode) or opened_identity != expected_identity:
        raise SnapshotImportPlanIntegrityError(
            f"{label} descriptor changed during import plan"
        )
    try:
        current = path.lstat()
    except OSError as error:
        raise SnapshotImportPlanIntegrityError(
            f"{label} changed during import plan: {path}"
        ) from error
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _complete_directory_identity(current) != expected_identity
    ):
        raise SnapshotImportPlanIntegrityError(
            f"{label} changed or was replaced: {path}"
        )
    return opened_identity


def _listing_token(
    present: Mapping[str, os.stat_result],
) -> Dict[str, Tuple[int, int, int, int, int, int]]:
    return {name: _complete_file_identity(info) for name, info in present.items()}


def _hash_held_fd(fd: int, *, path: Path, label: str) -> Tuple[str, int]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as error:
        raise SnapshotImportPlanIntegrityError(
            f"cannot rewind {label} {path}: {error}"
        ) from error
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(fd, HASH_CHUNK_BYTES)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    try:
        after = os.fstat(fd)
    except OSError as error:
        raise SnapshotImportPlanIntegrityError(
            f"{label} changed while it was read: {path}"
        ) from error
    if not stat.S_ISREG(after.st_mode) or total != after.st_size:
        raise SnapshotImportPlanIntegrityError(
            f"{label} changed while it was read: {path}"
        )
    return "sha256:" + digest.hexdigest(), total


def _read_held_bytes(fd: int, *, path: Path, label: str, max_bytes: Optional[int] = None) -> bytes:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError as error:
        raise SnapshotImportPlanIntegrityError(
            f"cannot rewind {label} {path}: {error}"
        ) from error
    chunks: List[bytes] = []
    total = 0
    while True:
        to_read = HASH_CHUNK_BYTES
        if max_bytes is not None:
            if total > max_bytes:
                raise SnapshotImportPlanError(
                    f"{label} exceeds bound {max_bytes} bytes: {path}"
                )
            to_read = min(HASH_CHUNK_BYTES, max_bytes + 1 - total)
        chunk = os.read(fd, to_read)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if max_bytes is not None and total > max_bytes:
        raise SnapshotImportPlanError(
            f"{label} exceeds bound {max_bytes} bytes: {path}"
        )
    return b"".join(chunks)


def _open_held_payload(
    directory_fd: int,
    name: str,
    path: Path,
    listed: os.stat_result,
) -> Tuple[int, Tuple[int, int, int, int, int, int], str, int]:
    try:
        fd, before = _open_regular_nofollow(
            directory_fd, name, path, label=name
        )
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _from_export_error(error) from error
    try:
        listed_id = _complete_file_identity(listed)
        before_id = _complete_file_identity(before)
        if before_id != listed_id:
            raise SnapshotImportPlanIntegrityError(
                f"payload {name} changed after the anchored listing"
            )
        revision, size = _hash_held_fd(fd, path=path, label=name)
        after = os.fstat(fd)
        after_id = _complete_file_identity(after)
        if after_id != before_id or size != before.st_size:
            raise SnapshotImportPlanIntegrityError(
                f"{name} changed while it was read: {path}"
            )
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotImportPlanIntegrityError(
                f"{name} changed while it was read: {path}"
            ) from error
        if (
            stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or _complete_file_identity(current) != before_id
        ):
            raise SnapshotImportPlanIntegrityError(
                f"{name} changed or became unsafe while it was read: {path}"
            )
        return fd, before_id, revision, size
    except Exception:
        os.close(fd)
        raise


def _reobserve_held_payload(
    directory_fd: int,
    name: str,
    path: Path,
    fd: int,
    expected_identity: Tuple[int, int, int, int, int, int],
    expected_revision: str,
) -> None:
    try:
        current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise SnapshotImportPlanIntegrityError(
            f"payload {name} disappeared during import plan: {path}"
        ) from error
    try:
        held = os.fstat(fd)
    except OSError as error:
        raise SnapshotImportPlanIntegrityError(
            f"cannot inspect held payload descriptor {path}: {error}"
        ) from error
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(current.st_mode)
        or not stat.S_ISREG(held.st_mode)
        or _complete_file_identity(current) != expected_identity
        or _complete_file_identity(held) != expected_identity
        or (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino)
    ):
        raise SnapshotImportPlanIntegrityError(
            f"payload {name} identity changed during import plan"
        )
    revision, size = _hash_held_fd(fd, path=path, label=name)
    if revision != expected_revision or size != expected_identity[2]:
        raise SnapshotImportPlanIntegrityError(
            f"payload {name} identity or content changed during import plan"
        )


def _load_parquet_rows(
    fd: int, *, path: Path, name: str
) -> Any:
    data = _read_held_bytes(fd, path=path, label=name)
    try:
        import pandas as pd
    except ImportError as error:
        raise SnapshotImportPlanError(
            f"pandas is required to read parquet: {error}"
        ) from error
    try:
        return pd.read_parquet(io.BytesIO(data))
    except Exception as error:
        raise SnapshotImportPlanIntegrityError(
            f"cannot read {name}: {path}: {error}"
        ) from error


def _parse_manifest(data: bytes, path: Path) -> Dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SnapshotImportPlanError(f"manifest is not valid UTF-8: {path}") from error
    try:
        parsed = _strict_json_loads(text)
    except (json.JSONDecodeError, ValueError) as error:
        raise SnapshotImportPlanError(f"manifest is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise SnapshotImportPlanError(f"manifest is not a JSON object: {path}")
    return parsed


def _observe_listing(
    export_dir: Path,
    directory_fd: int,
    expected_identity: Tuple[int, int, int, int],
) -> Dict[str, os.stat_result]:
    try:
        return _payload_children(export_dir, directory_fd, expected_identity)
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _from_export_error(error) from error


def _validate_source_envelope(
    export_dir: Path,
    payload_fds: Mapping[str, int],
    present: Mapping[str, os.stat_result],
    manifest: Mapping[str, Any],
) -> str:
    tables: Dict[str, Any] = {
        "entities": None,
        "relationships": None,
        "text_units": None,
        "call_observations": None,
    }
    name_map = {
        "entities.parquet": "entities",
        "relationships.parquet": "relationships",
        "text_units.parquet": "text_units",
        OBS_PARQUET: "call_observations",
    }
    for filename, key in name_map.items():
        if filename not in payload_fds:
            continue
        tables[key] = _load_parquet_rows(
            payload_fds[filename],
            path=export_dir / filename,
            name=filename,
        )
    empty: List[Any] = []
    snapshot_id = manifest.get("id") if isinstance(manifest.get("id"), str) else None
    report = validate_persisted_byog_snapshot(
        tables["entities"] if tables["entities"] is not None else empty,
        tables["relationships"] if tables["relationships"] is not None else empty,
        tables["text_units"] if tables["text_units"] is not None else empty,
        tables["call_observations"] if tables["call_observations"] is not None else empty,
        manifest,
        snapshot_id=snapshot_id,
        present_files=list(present),
        file_sizes={name: int(info.st_size) for name, info in present.items()},
    )
    if report.get("ok") is not True:
        raise SnapshotImportPlanIntegrityError(
            "source export persisted snapshot envelope is invalid"
        )
    if not isinstance(snapshot_id, str) or not is_published_snapshot_id(snapshot_id):
        raise SnapshotImportPlanIntegrityError(
            "manifest id is not a canonical published snapshot id"
        )
    try:
        planned = _planned_payload_names(manifest, present, resolved=snapshot_id)
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _from_export_error(error) from error
    if set(planned) != set(present):
        raise SnapshotImportPlanIntegrityError(
            "source export payload set is not the exact producer envelope"
        )
    return snapshot_id


def canonical_import_revision_payload(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Decision inputs bound by ``import_revision``.

    Compact UTF-8 JSON with sorted keys, no trailing newline binds the
    listed decision fields. Absolute graph/export paths, counts,
    notices, ``ok``, and the mutation flags are presentation-only. The
    complete ordered source file records are bound indirectly by
    ``source_export_revision``.
    """
    payload: Dict[str, Any] = {}
    for key in _IMPORT_REVISION_KEYS:
        if key not in result:
            raise SnapshotImportPlanError(
                f"import plan is missing revision input {key!r}"
            )
        payload[key] = result[key]

    schema = payload["schema_version"]
    if isinstance(schema, bool) or schema != PLAN_SCHEMA_VERSION_IMPORT:
        raise SnapshotImportPlanError("import plan schema_version must be 1")

    snapshot_id = payload["snapshot_id"]
    if not isinstance(snapshot_id, str) or not is_published_snapshot_id(snapshot_id):
        raise SnapshotImportPlanError(
            "import plan snapshot_id is not a canonical published id"
        )

    source_revision = payload["source_export_revision"]
    if not isinstance(source_revision, str) or not _REVISION_RE.fullmatch(source_revision):
        raise SnapshotImportPlanError(
            "import plan source_export_revision must be sha256:<64 lowercase hex>"
        )

    current = payload["current"]
    if not isinstance(current, str) or not is_published_snapshot_id(current):
        raise SnapshotImportPlanError(
            "import plan current is not a canonical published id"
        )

    published = payload["published_snapshots"]
    if not isinstance(published, list) or any(not isinstance(item, str) for item in published):
        raise SnapshotImportPlanError(
            "import plan published_snapshots must be a list of snapshot ids"
        )
    if any(not is_published_snapshot_id(item) for item in published):
        raise SnapshotImportPlanError(
            "import plan published_snapshots contains a non-canonical id"
        )
    if len(set(published)) != len(published) or list(published) != _byte_sort(published):
        raise SnapshotImportPlanError(
            "import plan published_snapshots must be unique and sorted in UTF-8-byte order"
        )
    if current not in published:
        raise SnapshotImportPlanError(
            "import plan current is not a member of published_snapshots"
        )

    staging_name = payload["target_staging_name"]
    expected_staging = f"{STAGING_NAME_PREFIX}{snapshot_id}"
    if (
        not isinstance(staging_name, str)
        or not _is_canonical_direct_name(staging_name)
        or staging_name != expected_staging
    ):
        raise SnapshotImportPlanError(
            "import plan target_staging_name must be exactly .staging-<snapshot-id>"
        )

    for flag in (
        "target_snapshot_present",
        "target_staging_present",
        "import_ready",
        "source_envelope_valid",
        "import_performed",
        "fresh_plan_required_before_import",
    ):
        value = payload[flag]
        if type(value) is not bool:
            raise SnapshotImportPlanError(
                f"import plan {flag} must be a JSON boolean, not {value!r}"
            )

    reasons = payload["blocking_reasons"]
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise SnapshotImportPlanError(
            "import plan blocking_reasons must be a list of strings"
        )
    if any(item not in _ALLOWED_BLOCKING for item in reasons):
        raise SnapshotImportPlanError(
            "import plan blocking_reasons contains an unsupported code"
        )
    if len(set(reasons)) != len(reasons) or list(reasons) != _byte_sort(reasons):
        raise SnapshotImportPlanError(
            "import plan blocking_reasons must be unique and sorted in UTF-8-byte order"
        )
    if payload["source_envelope_valid"] is not True:
        raise SnapshotImportPlanError("import plan source_envelope_valid must be true")
    if payload["import_performed"] is not False:
        raise SnapshotImportPlanError("import plan import_performed must be false")
    if payload["fresh_plan_required_before_import"] is not True:
        raise SnapshotImportPlanError(
            "import plan fresh_plan_required_before_import must be true"
        )
    expected_snapshot_present = snapshot_id in set(published)
    if payload["target_snapshot_present"] is not expected_snapshot_present:
        raise SnapshotImportPlanError(
            "import plan target_snapshot_present does not match published_snapshots"
        )
    expected_reasons: List[str] = []
    if expected_snapshot_present:
        expected_reasons.append(_BLOCK_PUBLISHED)
    if payload["target_staging_present"] is True:
        expected_reasons.append(_BLOCK_STAGING)
    expected_reasons = _byte_sort(expected_reasons)
    if list(reasons) != expected_reasons:
        raise SnapshotImportPlanError(
            "import plan blocking_reasons do not match the target presence flags"
        )
    expected_ready = not expected_reasons
    if payload["import_ready"] is not expected_ready:
        raise SnapshotImportPlanError(
            "import plan import_ready does not match its blocking reasons"
        )
    return payload


def canonical_import_revision_text(result: Mapping[str, Any]) -> str:
    return json.dumps(
        canonical_import_revision_payload(result),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def import_revision_of(result: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        canonical_import_revision_text(result).encode("utf-8")
    ).hexdigest()
    return "sha256:" + digest


def _after_import_source_path_inspected(_export_dir: Path) -> None:
    """Test hook after initial export-path validation and before descriptor open."""
    return None


def _after_import_graph_path_inspected(_graph: Path) -> None:
    """Test hook after initial graph-path validation and before the lease."""
    return None


def _after_import_source_directory_opened(
    _export_dir: Path, _directory_fd: int, _identity: Tuple[int, int, int, int, int]
) -> None:
    """Test hook after the export directory is anchored."""
    return None


def _after_import_source_listed(
    _export_dir: Path, _entries: Mapping[str, os.stat_result]
) -> None:
    """Test hook after the first anchored export listing."""
    return None


def _after_import_source_first_observation(
    _export_dir: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after the first complete source observation."""
    return None


def _after_import_source_second_listed(
    _export_dir: Path, _entries: Mapping[str, os.stat_result]
) -> None:
    """Test hook after the second-pass export listing."""
    return None


def _after_import_graph_tokens_captured(
    _root: Path, _tokens: Mapping[str, Any]
) -> None:
    """Test hook after the first target token capture."""
    return None


def _after_import_result_ready(
    _export_dir: Path,
    _graph: Path,
    _export_fd: int,
    _graph_fd: int,
    _payload_fds: Mapping[str, int],
    _result: Mapping[str, Any],
) -> None:
    """Test hook after the result is built and before it is yielded."""
    return None


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
    _after_import_graph_tokens_captured(root, first)
    second = _capture_target_tokens(root)
    if first != second:
        raise SnapshotImportPlanIntegrityError(
            "publication lock, current, snapshots listing, or staging changed "
            "during import plan"
        )
    inventory = first["inventory"]
    current = inventory.get("current")
    if not isinstance(current, str) or not is_published_snapshot_id(current):
        raise SnapshotImportPlanIntegrityError(
            f"current is not a canonical published snapshot id: {current!r}"
        )
    published = list(inventory.get("published_snapshots") or [])
    if current not in published:
        raise SnapshotImportPlanIntegrityError(
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
    unrelated_staging = [
        name for name in staging_names if name != target_staging_name
    ]
    return {
        "current": current,
        "published_snapshots": published,
        "target_staging_name": target_staging_name,
        "target_snapshot_present": target_snapshot_present,
        "target_staging_present": target_staging_present,
        "staging_count": len(staging_names),
        "blocking_reasons": blocking,
        "import_ready": (
            not target_snapshot_present
            and not target_staging_present
            and not blocking
        ),
        "unrelated_staging": unrelated_staging,
        "tokens": first,
    }


@contextmanager
def _held_standalone_export_observation(
    export_dir: Path,
    expected_path_identity: Tuple[int, int, int, int, int],
) -> Iterator[Dict[str, Any]]:
    """Observe one standalone export and keep its directory and payload fds."""
    try:
        _require_descriptor_reads()
    except SnapshotExportPlanError as error:
        raise SnapshotImportPlanError(str(error)) from error
    try:
        directory_fd, opened_export_identity = _open_directory_nofollow(
            export_dir, label="export directory"
        )
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _from_export_error(error) from error
    payload_fds: Dict[str, int] = {}
    try:
        opened = os.fstat(directory_fd)
        complete_dir = _complete_directory_identity(opened)
        if complete_dir != expected_path_identity:
            raise SnapshotImportPlanIntegrityError(
                "export directory changed before it was anchored"
            )
        _observe_held_directory(
            export_dir, directory_fd, complete_dir, label="export directory"
        )
        try:
            canonical_root = export_dir.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SnapshotImportPlanIntegrityError(
                f"export directory changed during canonicalization: {export_dir}"
            ) from error
        _observe_held_directory(
            canonical_root, directory_fd, complete_dir, label="export directory"
        )
        export_dir = canonical_root
        _after_import_source_directory_opened(export_dir, directory_fd, complete_dir)

        first_present = _observe_listing(export_dir, directory_fd, opened_export_identity)
        _after_import_source_listed(export_dir, first_present)
        if MANIFEST_NAME not in first_present:
            raise SnapshotImportPlanIntegrityError(
                "export directory is missing required payload manifest.json"
            )

        identities: Dict[str, Tuple[int, int, int, int, int, int]] = {}
        revisions: Dict[str, str] = {}
        sizes: Dict[str, int] = {}
        for name in _byte_sort(list(first_present)):
            fd, identity, revision, size = _open_held_payload(
                directory_fd,
                name,
                export_dir / name,
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
        _after_import_source_first_observation(export_dir, records)

        second_dir = _observe_held_directory(
            export_dir, directory_fd, complete_dir, label="export directory"
        )
        second_present = _observe_listing(export_dir, directory_fd, opened_export_identity)
        _after_import_source_second_listed(export_dir, second_present)
        if (
            second_dir != complete_dir
            or _listing_token(first_present) != _listing_token(second_present)
            or set(second_present) != set(payload_fds)
        ):
            raise SnapshotImportPlanIntegrityError(
                "export directory listing or payload set changed during import plan"
            )
        for name in _byte_sort(list(payload_fds)):
            _reobserve_held_payload(
                directory_fd,
                name,
                export_dir / name,
                payload_fds[name],
                identities[name],
                revisions[name],
            )
        final_dir = _observe_held_directory(
            export_dir, directory_fd, complete_dir, label="export directory"
        )
        final_present = _observe_listing(export_dir, directory_fd, opened_export_identity)
        if (
            final_dir != complete_dir
            or _listing_token(first_present) != _listing_token(final_present)
        ):
            raise SnapshotImportPlanIntegrityError(
                "export directory listing, manifest, or payload changed "
                "during import plan"
            )

        manifest_bytes = _read_held_bytes(
            payload_fds[MANIFEST_NAME],
            path=export_dir / MANIFEST_NAME,
            label="manifest",
            max_bytes=MAX_MANIFEST_BYTES,
        )
        manifest = _parse_manifest(manifest_bytes, export_dir / MANIFEST_NAME)
        snapshot_id = _validate_source_envelope(
            export_dir, payload_fds, first_present, manifest
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
                "resolved_snapshot": snapshot_id,
                "files": planned_records,
            }
        )
        yield {
            "export_directory": export_dir,
            "directory_fd": directory_fd,
            "directory_identity": complete_dir,
            "opened_export_identity": opened_export_identity,
            "payload_fds": payload_fds,
            "payload_identities": identities,
            "payload_revisions": revisions,
            "listing_token": _listing_token(first_present),
            "snapshot_id": snapshot_id,
            "files": planned_records,
            "source_export_revision": source_export_revision,
        }
    finally:
        for fd in payload_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        os.close(directory_fd)


def _open_anchored_graph(
    graph_path: Path,
    graph_path_identity: Tuple[int, int, int, int, int],
) -> Tuple[Path, int, Tuple[int, int, int, int, int]]:
    """Open and canonicalize one managed graph directory.

    Caller owns the returned directory descriptor and must close it.
    The shared existing-lock lease must already be held.
    """
    try:
        graph_fd, opened_graph_identity = _open_directory_nofollow(
            graph_path, label="graph root"
        )
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _from_export_error(error) from error
    try:
        opened_graph = os.fstat(graph_fd)
        graph_identity = _complete_directory_identity(opened_graph)
        if graph_identity != graph_path_identity:
            raise SnapshotImportPlanIntegrityError(
                "graph root changed before target observation"
            )
        _observe_held_directory(
            graph_path, graph_fd, graph_identity, label="graph root"
        )
        try:
            canonical_graph = graph_path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SnapshotImportPlanIntegrityError(
                f"graph root changed during canonicalization: {graph_path}"
            ) from error
        _observe_held_directory(
            canonical_graph, graph_fd, graph_identity, label="graph root"
        )
        if opened_graph_identity != (
            opened_graph.st_dev,
            opened_graph.st_ino,
            opened_graph.st_mtime_ns,
            opened_graph.st_mode,
        ):
            raise SnapshotImportPlanIntegrityError(
                "graph root changed before target observation"
            )
        return canonical_graph, graph_fd, graph_identity
    except Exception:
        os.close(graph_fd)
        raise


def _observe_fresh_import_plan(
    graph_path: Path,
    graph_fd: int,
    graph_identity: Tuple[int, int, int, int, int],
    source: Mapping[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build one schema-1 import plan from already-held source and graph.

    Caller must hold the shared existing-lock lease. Source directory and
    payload descriptors stay open. This is the same decision contract the
    public plan command emits; it is not a second path-based CLI invocation.
    """
    _observe_held_directory(
        graph_path, graph_fd, graph_identity, label="graph root"
    )
    target = _observe_target_unlocked(graph_path, source["snapshot_id"])
    _observe_held_directory(
        graph_path, graph_fd, graph_identity, label="graph root"
    )
    _observe_held_directory(
        source["export_directory"],
        source["directory_fd"],
        source["directory_identity"],
        label="export directory",
    )
    final_tokens = _capture_target_tokens(graph_path)
    if final_tokens != target["tokens"]:
        raise SnapshotImportPlanIntegrityError(
            "publication lock, current, snapshots listing, or "
            "staging changed during import plan"
        )
    _reobserve_held_source(source)
    result = _build_result(graph_path, source, target)
    return result, target


def _reobserve_held_source(source: Mapping[str, Any]) -> None:
    """Revalidate the complete source after target observation.

    The first two complete source observations precede the graph lease so
    corrupt exports fail without unnecessarily blocking publishers. This
    final observation closes the joint source/target consistency window.
    """
    export_dir = Path(source["export_directory"])
    directory_fd = int(source["directory_fd"])
    directory_identity = source["directory_identity"]
    opened_identity = source["opened_export_identity"]
    payload_fds = source["payload_fds"]
    identities = source["payload_identities"]
    revisions = source["payload_revisions"]
    _observe_held_directory(
        export_dir,
        directory_fd,
        directory_identity,
        label="export directory",
    )
    present = _observe_listing(export_dir, directory_fd, opened_identity)
    if (
        _listing_token(present) != source["listing_token"]
        or set(present) != set(payload_fds)
    ):
        raise SnapshotImportPlanIntegrityError(
            "export directory listing or payload set changed during target observation"
        )
    for name in _byte_sort(list(payload_fds)):
        _reobserve_held_payload(
            directory_fd,
            name,
            export_dir / name,
            payload_fds[name],
            identities[name],
            revisions[name],
        )
    _observe_held_directory(
        export_dir,
        directory_fd,
        directory_identity,
        label="export directory",
    )


def _build_result(
    graph: Path,
    source: Mapping[str, Any],
    target: Mapping[str, Any],
) -> Dict[str, Any]:
    notices = [dict(notice) for notice in _COMMAND_NOTICES]
    if target["unrelated_staging"]:
        notices.append(dict(_UNRELATED_STAGING_NOTICE))
    files = [dict(item) for item in source["files"]]
    result: Dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION_IMPORT,
        "ok": True,
        "graph": str(graph),
        "export_directory": str(source["export_directory"]),
        "snapshot_id": source["snapshot_id"],
        "source_export_revision": source["source_export_revision"],
        "files": files,
        "file_count": len(files),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in files),
        "source_envelope_valid": True,
        "current": target["current"],
        "published_snapshots": list(target["published_snapshots"]),
        "published_count": len(target["published_snapshots"]),
        "target_staging_name": target["target_staging_name"],
        "target_snapshot_present": target["target_snapshot_present"],
        "target_staging_present": target["target_staging_present"],
        "staging_count": target["staging_count"],
        "blocking_reasons": list(target["blocking_reasons"]),
        "import_ready": target["import_ready"],
        "import_performed": False,
        "graph_mutated": False,
        "export_mutated": False,
        "fresh_plan_required_before_import": True,
        "notices": notices,
    }
    result["import_revision"] = import_revision_of(result)
    return result


@contextmanager
def _snapshot_import_plan_scope(
    graph: Path, export_dir: Path
) -> Iterator[Dict[str, Any]]:
    """Yield one import plan while source descriptors and the graph lease are held."""
    source_path, source_path_identity = _resolve_export_dir(export_dir)
    graph_path, graph_path_identity = _resolve_graph_root(graph)
    with _held_standalone_export_observation(
        source_path, source_path_identity
    ) as source:
        try:
            _require_path_identity(
                graph_path, graph_path_identity, label="graph root"
            )
            with graph_read_lease(graph_path, allow_unlocked_managed=False):
                graph_path, graph_fd, graph_identity = _open_anchored_graph(
                    graph_path, graph_path_identity
                )
                try:
                    result, _target = _observe_fresh_import_plan(
                        graph_path,
                        graph_fd,
                        graph_identity,
                        source,
                    )
                    _after_import_result_ready(
                        source["export_directory"],
                        graph_path,
                        source["directory_fd"],
                        graph_fd,
                        source["payload_fds"],
                        result,
                    )
                    yield result
                finally:
                    os.close(graph_fd)
        except SnapshotImportPlanError:
            raise
        except SnapshotStagingError as error:
            raise _wrap_staging_error(error) from error
        except ByogPublicationLockError as error:
            raise _lock_error(error) from error
        except ByogReaderLockError as error:
            raise _lock_error(error) from error


def snapshot_import_plan(graph: Path, export_dir: Path) -> Dict[str, Any]:
    """Build one read-only import plan without writing files or streams."""
    with _snapshot_import_plan_scope(graph, export_dir) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report a read-only plan for adding one standalone snapshot "
            "export to an existing managed BYOG graph. Does not import, "
            "copy, activate, or mutate the graph or the export. This plan "
            "is not a backup and is not authorization to import or delete "
            "anything. Never creates .publish.lock, and is not an MCP tool. "
            "A fresh plan is required before any later import."
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
        "--export-dir",
        type=Path,
        required=True,
        help="Standalone export directory, relative to cwd.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_import_plan_scope(args.graph, args.export_dir) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing the graph lease and
            # source descriptors so the complete response is handed to the
            # caller under that protected interval.
            sys.stdout.flush()
    except SnapshotImportPlanError as error:
        print(f"snapshot-import-plan: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-import-plan: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
