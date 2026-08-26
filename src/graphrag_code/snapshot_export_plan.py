#!/usr/bin/env python
"""Read-only snapshot export plan.

``snapshot-export-plan`` inspects one retained published snapshot and
emits a deterministic schema-1 plan of its direct envelope payload
files. It does not create an archive, output directory, temp file,
lock, pin, snapshot, or registry entry. It does not mutate the graph
or the selected snapshot. ``export_performed`` is always false. The
plan is not a backup and is not authorization to delete anything.

``--snapshot`` is required and accepts exactly ``current`` or a
canonical retained published snapshot id. ``current`` is resolved once
under the protected interval. Managed snapshot graphs with an
already-adopted regular ``.publish.lock`` only. The command never
creates, truncates, chmods, rewrites, or replaces that lock. Legacy
flat and unlocked compatibility are out of scope.

One shared existing-lock graph lease covers snapshot selection,
validation, hashing, result construction, serialization, stdout write,
and stdout flush. The command does not call a public scope that takes
a nested graph lease. The selected directory stays anchored by a
no-follow descriptor, and payload files are opened relative to it and
hashed twice by bounded-memory streaming. A platform missing those safe
descriptor primitives is rejected. MCP stays exactly 13 read-only
tools; this command is CLI-only.

``export_revision`` is a self-consistency token for this exact observed
payload. It does not prove provenance, authenticity, recoverability,
or that a future export apply may proceed without a fresh plan.

Usage:
    graphrag-code snapshot-export-plan --graph <root> --snapshot <id|current> [--json]
    python -m graphrag_code.snapshot_export_plan --graph <root> --snapshot <id|current> [--json]
    uv run python scripts/snapshot_export_plan.py --graph <root> --snapshot <id|current> [--json]
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
    ByogPublicationLockError,
    ByogReaderLockError,
    _validate_managed_snapshot_layout,
    graph_read_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
)
from graphrag_code.byog_snapshot_integrity import (
    MANIFEST_NAME,
    OBS_PARQUET,
    REQUIRED_PARQUETS,
    SETTINGS_NAME,
)
from graphrag_code.snapshot_read import (
    CURRENT_REF,
    SnapshotReadError,
    parse_snapshot_ref,
)
from graphrag_code.snapshot_staging import (
    MAX_PUBLISHED_SNAPSHOTS,
    MAX_STAGING_ENTRIES,
    MAX_TOP_LEVEL_ENTRIES,
    SnapshotStagingError,
    SnapshotStagingIntegrityError,
    _lock_identity,
    _read_current,
    _safe_directory_entries,
)

PLAN_SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
HASH_CHUNK_BYTES = 64 * 1024
ACCEPTED_PAYLOAD_FILES = frozenset(
    REQUIRED_PARQUETS + (OBS_PARQUET, MANIFEST_NAME, SETTINGS_NAME)
)
REQUIRED_PAYLOAD_FILES = frozenset(REQUIRED_PARQUETS + (MANIFEST_NAME,))
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "plan_is_not_export",
        "kind": "notice",
        "message": (
            "snapshot-export-plan is inspection only. export_performed is "
            "always false. It does not create an archive, output directory, "
            "temp file, lock, pin, snapshot, or registry entry."
        ),
    },
    {
        "code": "plan_is_not_backup",
        "kind": "notice",
        "message": (
            "This plan is not a backup, archive, restore kit, or "
            "authorization to delete anything. It does not claim "
            "portability, recoverability, atomic backup, durability, "
            "or authenticity."
        ),
    },
    {
        "code": "export_revision_is_self_consistency_only",
        "kind": "notice",
        "message": (
            "export_revision is a self-consistency token for this exact "
            "observed payload. A future export apply must capture a fresh "
            "plan. Self-hashes do not prove provenance."
        ),
    },
    {
        "code": "fresh_plan_required_before_export",
        "kind": "notice",
        "message": (
            "fresh_plan_required_before_export is always true. This "
            "command is not an export apply and does not accept "
            "export_revision as authorization to copy files."
        ),
    },
    {
        "code": "advisory_locks_cooperating_only",
        "kind": "notice",
        "message": (
            "Advisory locks protect only cooperating processes. "
            "Two complete payload hash observations plus identity and listing "
            "rechecks detect differences visible across those reads. This is "
            "not continuous protection against lock-ignoring actors, and "
            "changes after the final observation are not covered."
        ),
    },
    {
        "code": "cli_only_not_mcp",
        "kind": "notice",
        "message": (
            "snapshot-export-plan is CLI-only and intentionally absent "
            "from the fixed 13-tool MCP set."
        ),
    },
)


class SnapshotExportPlanError(Exception):
    """Expected export-plan failure. Default exit 2."""

    exit_code = 2


class SnapshotExportPlanIntegrityError(SnapshotExportPlanError):
    """Unsafe structure or lock-ignoring change during the plan. Exit 1."""

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
    return (
        "snapshot-export-plan: "
        f"graph={result.get('graph')} "
        f"requested={result.get('requested_snapshot')} "
        f"resolved={result.get('resolved_snapshot')} "
        f"files={result.get('file_count')} "
        f"total_size_bytes={result.get('total_size_bytes')} "
        f"export_revision={result.get('export_revision')} "
        "export_performed=false "
        "fresh_plan_required_before_export=true "
        "This plan is not a backup and is not authorization to delete anything."
    )


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def _reject_nonstandard_json_constant(value: str) -> None:
    raise ValueError(f"nonstandard JSON constant {value!r}")


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotExportPlanError(f"graph root does not exist: {path}") from error
    except OSError as error:
        raise SnapshotExportPlanError(
            f"cannot inspect graph root {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotExportPlanError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotExportPlanError(f"graph root is not a real directory: {path}")
    return path.resolve()


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotExportPlanError(str(error)) from error
    if not managed:
        raise SnapshotExportPlanError(
            "legacy flat-parquet directory has no retained snapshot to export: "
            f"{root}"
        )


def _lock_error(error: Exception) -> SnapshotExportPlanError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotExportPlanError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotExportPlanIntegrityError(message)
    return SnapshotExportPlanError(message)


def _wrap_staging_error(error: Exception) -> SnapshotExportPlanError:
    if isinstance(error, SnapshotStagingIntegrityError):
        return SnapshotExportPlanIntegrityError(str(error))
    if isinstance(error, SnapshotStagingError):
        wrapped = SnapshotExportPlanError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    return SnapshotExportPlanError(str(error))


def _parse_snapshot(value: object) -> str:
    try:
        parsed = parse_snapshot_ref(value if isinstance(value, str) else None)
    except SnapshotReadError as error:
        raise SnapshotExportPlanError(str(error)) from error
    if parsed is None:
        raise SnapshotExportPlanError(
            "snapshot is required and must be current or a published snapshot id"
        )
    return parsed


def _is_canonical_direct_name(name: str) -> bool:
    return (
        bool(name)
        and name not in {".", ".."}
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
        and Path(name).name == name
    )


def _require_descriptor_reads() -> None:
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_dir_fd", set())
        or os.scandir not in getattr(os, "supports_fd", set())
    ):
        raise SnapshotExportPlanError(
            "safe descriptor-relative no-follow snapshot reads are unsupported on "
            f"this platform: {sys.platform!r}"
        )


def _file_identity(info: os.stat_result) -> Tuple[int, int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode)


def _directory_identity(info: os.stat_result) -> Tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_mode)


def _dir_identity(
    path: Path, *, label: str, missing_is_integrity: bool = False
) -> Tuple[int, int, int, int]:
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        if missing_is_integrity:
            raise SnapshotExportPlanIntegrityError(
                f"{label} disappeared during export plan: {path}"
            ) from error
        raise SnapshotExportPlanError(f"{label} does not exist: {path}") from error
    except OSError as error:
        raise SnapshotExportPlanError(
            f"cannot inspect {label} {path}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotExportPlanIntegrityError(f"unsafe symlinked {label}: {path}")
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotExportPlanError(f"{label} is not a real directory: {path}")
    return _directory_identity(info)


def _open_directory_nofollow(
    path: Path, *, label: str
) -> Tuple[int, Tuple[int, int, int, int]]:
    _require_descriptor_reads()
    before_identity = _dir_identity(path, label=label, missing_is_integrity=True)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(path), flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise SnapshotExportPlanIntegrityError(
                f"{label} changed or became unsafe while opening it: {path}"
            ) from error
        raise SnapshotExportPlanError(
            f"cannot safely open {label} {path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = path.lstat()
        except OSError as error:
            raise SnapshotExportPlanIntegrityError(
                f"{label} changed while opening it: {path}"
            ) from error
        opened_identity = _directory_identity(opened)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISDIR(current.st_mode)
            or opened_identity != before_identity
            or _directory_identity(current) != opened_identity
        ):
            raise SnapshotExportPlanIntegrityError(
                f"{label} changed or became unsafe while opening it: {path}"
            )
    except Exception:
        os.close(fd)
        raise
    return fd, opened_identity


def _after_export_tokens_captured(_root: Path, _tokens: Mapping[str, Any]) -> None:
    """Test hook after the first identity capture and before payload hashing."""
    return None


def _after_export_payload_listed(
    _root: Path, _entries: Mapping[str, os.stat_result]
) -> None:
    """Test hook after the anchored payload listing and before hashing."""
    return None


def _after_export_files_hashed(_root: Path, _records: Sequence[Mapping[str, Any]]) -> None:
    """Test hook after payload hashing and before the final identity recheck."""
    return None


def _list_snapshots_entries(root: Path) -> List[Dict[str, Any]]:
    snapshots = root / "snapshots"
    try:
        _info, entries = _safe_directory_entries(
            snapshots,
            max_entries=MAX_PUBLISHED_SNAPSHOTS + MAX_STAGING_ENTRIES,
            label="snapshots directory",
        )
    except SnapshotStagingError as error:
        raise _wrap_staging_error(error) from error
    listed: List[Dict[str, Any]] = []
    published = 0
    staging = 0
    for name, entry in entries:
        path = snapshots / name
        if stat.S_ISLNK(entry.st_mode):
            raise SnapshotExportPlanIntegrityError(
                f"unsafe symlinked snapshot entry: {path}"
            )
        if is_staging_snapshot_name(name):
            if not stat.S_ISDIR(entry.st_mode):
                raise SnapshotExportPlanError(
                    f"staging path is not a directory: {path}"
                )
            staging += 1
            if staging > MAX_STAGING_ENTRIES:
                raise SnapshotExportPlanError(
                    f"staging entry count exceeds bound {MAX_STAGING_ENTRIES}"
                )
            listed.append(
                {
                    "name": name,
                    "kind": "staging",
                    "dev": entry.st_dev,
                    "ino": entry.st_ino,
                }
            )
            continue
        if is_published_snapshot_id(name) and stat.S_ISDIR(entry.st_mode):
            published += 1
            if published > MAX_PUBLISHED_SNAPSHOTS:
                raise SnapshotExportPlanError(
                    "published snapshot count exceeds bound "
                    f"{MAX_PUBLISHED_SNAPSHOTS}"
                )
            listed.append(
                {
                    "name": name,
                    "kind": "published",
                    "dev": entry.st_dev,
                    "ino": entry.st_ino,
                }
            )
            continue
        raise SnapshotExportPlanError(
            f"unexpected unsafe snapshots entry is not published history: {path}"
        )
    listed.sort(key=lambda item: str(item["name"]).encode("utf-8"))
    return listed


def _open_regular_nofollow(
    directory_fd: int,
    name: str,
    path: Path,
    *,
    label: str,
) -> Tuple[int, os.stat_result]:
    _require_descriptor_reads()
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError as error:
        raise SnapshotExportPlanIntegrityError(
            f"{label} disappeared during export plan: {path}"
        ) from error
    except OSError as error:
        raise SnapshotExportPlanError(f"cannot inspect {label} {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotExportPlanIntegrityError(
            f"unsafe symlinked snapshot payload: {path}"
        )
    if not stat.S_ISREG(before.st_mode):
        raise SnapshotExportPlanError(
            f"snapshot payload is not a regular file: {path}"
        )
    flags = os.O_RDONLY | os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT}:
            raise SnapshotExportPlanIntegrityError(
                f"{label} changed or became unsafe while opening it: {path}"
            ) from error
        raise SnapshotExportPlanError(
            f"cannot safely open {label} {path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        try:
            current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise SnapshotExportPlanIntegrityError(
                f"{label} changed while opening it: {path}"
            ) from error
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise SnapshotExportPlanIntegrityError(
                f"{label} changed or became unsafe while opening it: {path}"
            )
    except Exception:
        os.close(fd)
        raise
    return fd, before


def _stream_regular_file(
    directory_fd: int,
    name: str,
    path: Path,
    *,
    label: str,
    max_bytes: Optional[int] = None,
) -> Tuple[bytes, str, Tuple[int, int, int, int, int]]:
    fd, before = _open_regular_nofollow(
        directory_fd, name, path, label=label
    )
    digest = hashlib.sha256()
    chunks: List[bytes] = []
    total = 0
    try:
        while True:
            to_read = HASH_CHUNK_BYTES
            if max_bytes is not None:
                if total > max_bytes:
                    raise SnapshotExportPlanError(
                        f"{label} exceeds bound {max_bytes} bytes: {path}"
                    )
                to_read = min(HASH_CHUNK_BYTES, max_bytes + 1 - total)
            chunk = os.read(fd, to_read)
            if not chunk:
                break
            digest.update(chunk)
            if max_bytes is not None:
                chunks.append(chunk)
            total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise SnapshotExportPlanError(
                f"{label} exceeds bound {max_bytes} bytes: {path}"
            )
        after_fd = os.fstat(fd)
        try:
            after_path = os.stat(
                name, dir_fd=directory_fd, follow_symlinks=False
            )
        except OSError as error:
            raise SnapshotExportPlanIntegrityError(
                f"{label} changed while it was read: {path}"
            ) from error
        before_id = _file_identity(before)
        if (
            _file_identity(after_fd) != before_id
            or stat.S_ISLNK(after_path.st_mode)
            or not stat.S_ISREG(after_path.st_mode)
            or _file_identity(after_path) != before_id
            or total != before.st_size
        ):
            raise SnapshotExportPlanIntegrityError(
                f"{label} changed while it was read: {path}"
            )
    finally:
        os.close(fd)
    content = b"".join(chunks) if max_bytes is not None else b""
    return content, "sha256:" + digest.hexdigest(), before_id


def _load_manifest(
    directory_fd: int, path: Path
) -> Tuple[Dict[str, Any], str, Tuple[int, int, int, int, int]]:
    data, revision, identity = _stream_regular_file(
        directory_fd,
        MANIFEST_NAME,
        path,
        label="manifest",
        max_bytes=MAX_MANIFEST_BYTES,
    )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SnapshotExportPlanError(f"manifest is not valid UTF-8: {path}") from error
    try:
        parsed = json.loads(text, parse_constant=_reject_nonstandard_json_constant)
    except (json.JSONDecodeError, ValueError) as error:
        raise SnapshotExportPlanError(f"manifest is not valid JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise SnapshotExportPlanError(f"manifest is not a JSON object: {path}")
    return parsed, revision, identity


def _payload_children(
    snap_dir: Path,
    directory_fd: int,
    expected_directory_identity: Tuple[int, int, int, int],
) -> Dict[str, os.stat_result]:
    _require_descriptor_reads()
    try:
        before = os.fstat(directory_fd)
    except OSError as error:
        raise SnapshotExportPlanIntegrityError(
            f"cannot inspect selected snapshot descriptor {snap_dir}: {error}"
        ) from error
    if (
        not stat.S_ISDIR(before.st_mode)
        or _directory_identity(before) != expected_directory_identity
    ):
        raise SnapshotExportPlanIntegrityError(
            "selected snapshot descriptor changed during export plan"
        )
    children: Dict[str, os.stat_result] = {}
    try:
        with os.scandir(directory_fd) as iterator:
            for entry in iterator:
                if len(children) >= MAX_TOP_LEVEL_ENTRIES:
                    raise SnapshotExportPlanError(
                        "selected snapshot directory entry count exceeds bound "
                        f"{MAX_TOP_LEVEL_ENTRIES}: {snap_dir}"
                    )
                name = entry.name
                path = snap_dir / name
                if not _is_canonical_direct_name(name):
                    raise SnapshotExportPlanError(
                        "selected snapshot contains a non-canonical direct name: "
                        f"{name!r}"
                    )
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError as error:
                    raise SnapshotExportPlanIntegrityError(
                        f"cannot inspect selected snapshot payload {path}: {error}"
                    ) from error
                if stat.S_ISLNK(info.st_mode):
                    raise SnapshotExportPlanIntegrityError(
                        f"unsafe symlinked snapshot payload: {path}"
                    )
                if name not in ACCEPTED_PAYLOAD_FILES:
                    raise SnapshotExportPlanError(
                        "unexpected snapshot entry is not an envelope payload file: "
                        f"{path}"
                    )
                if not stat.S_ISREG(info.st_mode):
                    raise SnapshotExportPlanError(
                        f"snapshot payload is not a regular file: {path}"
                    )
                children[name] = info
    except SnapshotExportPlanError:
        raise
    except OSError as error:
        raise SnapshotExportPlanIntegrityError(
            f"cannot list selected snapshot directory {snap_dir}: {error}"
        ) from error
    after = os.fstat(directory_fd)
    if _directory_identity(after) != expected_directory_identity:
        raise SnapshotExportPlanIntegrityError(
            "selected snapshot directory metadata changed while it was listed"
        )
    return children


def _planned_payload_names(
    manifest: Mapping[str, Any],
    present: Mapping[str, os.stat_result],
    *,
    resolved: str,
) -> List[str]:
    missing = [name for name in _byte_sort(REQUIRED_PAYLOAD_FILES) if name not in present]
    if missing:
        raise SnapshotExportPlanError(
            f"selected snapshot is missing required payload {missing[0]}"
        )
    declared = manifest.get("files")
    if not isinstance(declared, list) or any(not isinstance(item, str) for item in declared):
        raise SnapshotExportPlanError(
            "manifest files must be the ordered producer parquet list"
        )
    for name in declared:
        if not _is_canonical_direct_name(name) or name not in ACCEPTED_PAYLOAD_FILES:
            raise SnapshotExportPlanError(
                f"manifest-declared filename is not a canonical direct payload name: {name!r}"
            )
    expected = list(REQUIRED_PARQUETS)
    if OBS_PARQUET in present:
        expected.append(OBS_PARQUET)
    if list(declared) != expected:
        raise SnapshotExportPlanError(
            "manifest files is not the exact ordered producer parquet list "
            f"for the present payload: {list(declared)!r} != {expected!r}"
        )
    snap_id = manifest.get("id")
    if not isinstance(snap_id, str) or not is_published_snapshot_id(snap_id):
        raise SnapshotExportPlanError("manifest id is not a canonical published snapshot id")
    if snap_id != resolved:
        raise SnapshotExportPlanIntegrityError(
            "manifest.id differs from the selected snapshot directory"
        )
    schema = manifest.get("schema_version")
    if isinstance(schema, bool) or schema != PLAN_SCHEMA_VERSION:
        raise SnapshotExportPlanError("manifest schema_version must be the strict integer 1")
    planned = [MANIFEST_NAME, *expected]
    if SETTINGS_NAME in present:
        planned.append(SETTINGS_NAME)
    return _byte_sort(planned)


def canonical_export_revision_payload(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Decision inputs bound by ``export_revision``.

    Compact UTF-8 JSON with sorted keys, no trailing newline binds
    ``schema_version``, ``resolved_snapshot``, and the complete ordered
    ``files`` records (``path``, ``size_bytes``, ``content_revision``).
    Graph and snapshot absolute paths, ``requested_snapshot``,
    ``file_count``, ``total_size_bytes``, ``ok``, notices, and the
    boolean flags are presentation-only.
    """
    files = result.get("files")
    if not isinstance(files, list):
        raise SnapshotExportPlanError("export plan is missing files")
    if len(files) > len(ACCEPTED_PAYLOAD_FILES):
        raise SnapshotExportPlanError("export plan has too many file records")
    records: List[Dict[str, Any]] = []
    for item in files:
        if not isinstance(item, dict):
            raise SnapshotExportPlanError("export plan file record is malformed")
        for key in ("path", "size_bytes", "content_revision"):
            if key not in item:
                raise SnapshotExportPlanError(
                    f"export plan file record is missing {key}"
                )
        path = item["path"]
        size = item["size_bytes"]
        revision = item["content_revision"]
        if (
            not isinstance(path, str)
            or not _is_canonical_direct_name(path)
            or path not in ACCEPTED_PAYLOAD_FILES
        ):
            raise SnapshotExportPlanError(
                f"export plan file path is not a direct envelope name: {path!r}"
            )
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise SnapshotExportPlanError(
                f"export plan file size is not a non-negative integer: {size!r}"
            )
        if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
            raise SnapshotExportPlanError(
                "export plan content_revision must be sha256:<64 lowercase hex>"
            )
        records.append(
            {
                "content_revision": revision,
                "path": path,
                "size_bytes": size,
            }
        )
    resolved = result.get("resolved_snapshot")
    if not isinstance(resolved, str) or not is_published_snapshot_id(resolved):
        raise SnapshotExportPlanError(
            "export plan resolved_snapshot is not a canonical published id"
        )
    paths = [str(item["path"]) for item in records]
    if len(set(paths)) != len(paths) or paths != _byte_sort(paths):
        raise SnapshotExportPlanError(
            "export plan files must be unique and sorted in UTF-8-byte order"
        )
    schema = result.get("schema_version")
    if isinstance(schema, bool) or schema != PLAN_SCHEMA_VERSION:
        raise SnapshotExportPlanError("export plan schema_version must be 1")
    return {
        "files": records,
        "resolved_snapshot": resolved,
        "schema_version": schema,
    }


def canonical_export_revision_text(result: Mapping[str, Any]) -> str:
    return json.dumps(
        canonical_export_revision_payload(result),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def export_revision_of(result: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        canonical_export_revision_text(result).encode("utf-8")
    ).hexdigest()
    return "sha256:" + digest


def _capture_tokens(
    root: Path,
    *,
    requested: str,
    resolved: Optional[str],
) -> Dict[str, Any]:
    try:
        lock_identity = _lock_identity(root)
    except SnapshotStagingError as error:
        raise _wrap_staging_error(error) from error
    current_value: Optional[str] = None
    current_identity: Optional[Tuple[int, int, int, int]] = None
    if requested == CURRENT_REF:
        try:
            current_value, current_identity = _read_current(root)
        except SnapshotStagingError as error:
            raise _wrap_staging_error(error) from error
    listing = _list_snapshots_entries(root)
    selected_identity: Optional[Tuple[int, int, int, int]] = None
    if resolved is not None:
        selected_identity = _dir_identity(
            root / "snapshots" / resolved,
            label="selected snapshot directory",
            missing_is_integrity=True,
        )
    return {
        "lock_identity": lock_identity,
        "current_value": current_value,
        "current_identity": current_identity,
        "listing": listing,
        "selected_identity": selected_identity,
    }


@contextmanager
def _held_snapshot_export_plan_unlocked(
    root: Path, requested: str
) -> Iterator[
    Tuple[Dict[str, Any], int, Tuple[int, int, int, int], Dict[str, Any]]
]:
    """Build one export plan and keep the selected-directory descriptor.

    The descriptor stays open until the context exits so a later export
    apply can copy from the same anchored inode. The yielded token set
    is the plan's final lock/current/listing/selected-directory
    observation, captured after the second payload-hash pass. Closing
    still happens here; callers must not close the fd. Public
    ``_build_plan_unlocked`` and ``snapshot_export_plan`` keep their
    previous close-before-return behavior.
    """
    _require_managed_graph(root)
    _dir_identity(root / "snapshots", label="snapshots directory")
    first = _capture_tokens(root, requested=requested, resolved=None)
    if requested == CURRENT_REF:
        resolved = str(first["current_value"])
    else:
        resolved = requested
        names = {item["name"] for item in first["listing"] if item["kind"] == "published"}
        if resolved not in names:
            raise SnapshotExportPlanError(
                f"snapshot is not a retained published directory: {resolved!r}"
            )
    first["selected_identity"] = _dir_identity(
        root / "snapshots" / resolved,
        label="selected snapshot directory",
    )
    snap_dir = root / "snapshots" / resolved
    directory_fd, opened_directory_identity = _open_directory_nofollow(
        snap_dir, label="selected snapshot directory"
    )
    try:
        if opened_directory_identity != first["selected_identity"]:
            raise SnapshotExportPlanIntegrityError(
                "selected snapshot directory changed before payload inspection"
            )
        _after_export_tokens_captured(root, first)
        present = _payload_children(
            snap_dir, directory_fd, opened_directory_identity
        )
        _after_export_payload_listed(root, present)
        if MANIFEST_NAME not in present:
            raise SnapshotExportPlanError(
                "selected snapshot is missing required payload manifest.json"
            )
        manifest, manifest_revision, manifest_identity = _load_manifest(
            directory_fd, snap_dir / MANIFEST_NAME
        )
        if manifest_identity != _file_identity(present[MANIFEST_NAME]):
            raise SnapshotExportPlanIntegrityError(
                "manifest changed after the anchored payload listing"
            )
        planned = _planned_payload_names(manifest, present, resolved=resolved)
        records: List[Dict[str, Any]] = []
        identities: Dict[str, Tuple[int, int, int, int, int]] = {
            MANIFEST_NAME: manifest_identity
        }
        revisions: Dict[str, str] = {MANIFEST_NAME: manifest_revision}
        for name in planned:
            path = snap_dir / name
            if name == MANIFEST_NAME:
                records.append(
                    {
                        "path": name,
                        "size_bytes": manifest_identity[2],
                        "content_revision": manifest_revision,
                    }
                )
                continue
            _data, revision, identity = _stream_regular_file(
                directory_fd, name, path, label=name
            )
            if identity != _file_identity(present[name]):
                raise SnapshotExportPlanIntegrityError(
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
        _after_export_files_hashed(root, records)
        second = _capture_tokens(root, requested=requested, resolved=resolved)
        if (
            first["lock_identity"] != second["lock_identity"]
            or first["current_value"] != second["current_value"]
            or first["current_identity"] != second["current_identity"]
            or first["listing"] != second["listing"]
            or first["selected_identity"] != second["selected_identity"]
        ):
            raise SnapshotExportPlanIntegrityError(
                "publication lock, current, snapshots listing, or selected "
                "snapshot changed during export plan"
            )
        later_present = _payload_children(
            snap_dir, directory_fd, opened_directory_identity
        )
        if set(later_present) != set(present):
            raise SnapshotExportPlanIntegrityError(
                "selected snapshot payload set changed during export plan"
            )
        later_manifest, later_revision, later_identity = _load_manifest(
            directory_fd, snap_dir / MANIFEST_NAME
        )
        if later_identity != manifest_identity or later_revision != manifest_revision:
            raise SnapshotExportPlanIntegrityError(
                "manifest identity or content changed during export plan"
            )
        later_planned = _planned_payload_names(
            later_manifest, later_present, resolved=resolved
        )
        if later_planned != planned:
            raise SnapshotExportPlanIntegrityError(
                "manifest payload set changed during export plan"
            )
        for name in planned:
            if name == MANIFEST_NAME:
                continue
            _data, later_revision, later_identity = _stream_regular_file(
                directory_fd, name, snap_dir / name, label=name
            )
            if (
                later_identity != identities[name]
                or later_revision != revisions[name]
                or later_identity != _file_identity(later_present[name])
            ):
                raise SnapshotExportPlanIntegrityError(
                    f"payload {name} identity or content changed during export plan"
                )
        result: Dict[str, Any] = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "ok": True,
            "graph": str(root),
            "requested_snapshot": requested,
            "resolved_snapshot": resolved,
            "snapshot_path": str(snap_dir),
            "files": records,
            "file_count": len(records),
            "total_size_bytes": sum(int(item["size_bytes"]) for item in records),
            "export_performed": False,
            "fresh_plan_required_before_export": True,
            "notices": [dict(notice) for notice in _COMMAND_NOTICES],
        }
        result["export_revision"] = export_revision_of(result)
        final = _capture_tokens(root, requested=requested, resolved=resolved)
        if (
            first["lock_identity"] != second["lock_identity"]
            or first["current_value"] != second["current_value"]
            or first["current_identity"] != second["current_identity"]
            or first["listing"] != second["listing"]
            or first["selected_identity"] != second["selected_identity"]
            or first["lock_identity"] != final["lock_identity"]
            or first["current_value"] != final["current_value"]
            or first["current_identity"] != final["current_identity"]
            or first["listing"] != final["listing"]
            or first["selected_identity"] != final["selected_identity"]
            or final["selected_identity"] != opened_directory_identity
        ):
            raise SnapshotExportPlanIntegrityError(
                "publication lock, current, snapshots listing, or selected "
                "snapshot changed during export plan"
            )
        if requested == CURRENT_REF and final["current_value"] != resolved:
            raise SnapshotExportPlanIntegrityError(
                "current no longer names the selected snapshot"
            )
        yield result, directory_fd, opened_directory_identity, final
    finally:
        os.close(directory_fd)


def _build_plan_unlocked(root: Path, requested: str) -> Dict[str, Any]:
    with _held_snapshot_export_plan_unlocked(root, requested) as (
        result,
        _fd,
        _identity,
        _tokens,
    ):
        return result


@contextmanager
def _snapshot_export_plan_scope(
    graph: Path, snapshot: object
) -> Iterator[Dict[str, Any]]:
    """Yield one export plan while its shared existing-lock lease remains held."""
    requested = _parse_snapshot(snapshot)
    root = _resolve_graph_root(graph)
    try:
        with graph_read_lease(root, allow_unlocked_managed=False):
            yield _build_plan_unlocked(root, requested)
    except SnapshotExportPlanError:
        raise
    except SnapshotStagingError as error:
        raise _wrap_staging_error(error) from error
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_export_plan(graph: Path, snapshot: str) -> Dict[str, Any]:
    """Build one read-only export plan without writing files or streams."""
    with _snapshot_export_plan_scope(graph, snapshot) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report a read-only export plan for one retained published "
            "snapshot. Does not create an archive, copy files, or mutate "
            "the graph. This plan is not a backup and is not authorization "
            "to delete anything. Never creates .publish.lock, and is not "
            "an MCP tool. A fresh plan is required before any later export."
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
        "--snapshot",
        required=True,
        help="current or a canonical retained published snapshot id.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_export_plan_scope(args.graph, args.snapshot) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing the graph lease so
            # the complete response is handed to the caller under that lease.
            sys.stdout.flush()
    except SnapshotExportPlanError as error:
        print(f"snapshot-export-plan: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-export-plan: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
