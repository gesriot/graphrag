#!/usr/bin/env python
"""Read-only standalone snapshot export verification.

``snapshot-export-verify`` inspects one already-created standalone
export directory and reports whether it still contains exactly the
snapshot envelope bound by ``--expected-export-revision``. It verifies
bytes and structure only. It does not inspect a managed graph, acquire
a graph lease, or mutate the export directory or any graph. It does
not create, copy, rename, repair, quarantine, import, restore, or
delete anything. It does not claim that the export is a backup,
authentic, recoverable, complete source evidence, or authorization to
delete anything. MCP stays exactly 15 read-only tools; this command is
CLI-only.

``--export-dir`` and ``--expected-export-revision`` are required. The
expected revision is exactly ``sha256:<64 lowercase hex>`` with no
whitespace normalization. Relative paths resolve from the invoking
cwd. The final export path must be an existing real directory, not a
symlink. Observed ``export_revision`` is computed through the
snapshot-export-plan canonical helpers and is byte-for-byte compatible
with snapshot-export-plan and snapshot-export-apply.

Usage:
    graphrag-code snapshot-export-verify --export-dir <directory> \\
        --expected-export-revision sha256:<hex> [--json]
    python -m graphrag_code.snapshot_export_verify --export-dir <directory> \\
        --expected-export-revision sha256:<hex> [--json]
    uv run python scripts/snapshot_export_verify.py --export-dir <directory> \\
        --expected-export-revision sha256:<hex> [--json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from graphrag_code.byog_snapshot_integrity import MANIFEST_NAME
from graphrag_code.snapshot_export_plan import (
    PLAN_SCHEMA_VERSION,
    SnapshotExportPlanError,
    SnapshotExportPlanIntegrityError,
    _directory_identity,
    _file_identity,
    _load_manifest,
    _open_directory_nofollow,
    _payload_children,
    _planned_payload_names,
    _require_descriptor_reads,
    _stream_regular_file,
    export_revision_of,
)

VERIFY_SCHEMA_VERSION = 1
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "verify_is_not_backup",
        "kind": "notice",
        "message": (
            "snapshot-export-verify checks bytes and structure only. "
            "It is not a backup, authenticity, recoverability, or "
            "complete-source-evidence claim, and it is not authorization "
            "to delete anything."
        ),
    },
    {
        "code": "export_revision_is_self_consistency_only",
        "kind": "notice",
        "message": (
            "observed_export_revision uses the snapshot-export-plan "
            "canonical export-revision contract. A matching token does "
            "not prove provenance, authenticity, or recoverability."
        ),
    },
    {
        "code": "export_not_mutated",
        "kind": "notice",
        "message": (
            "This command does not mutate the export directory or any "
            "graph. export_mutated is always false and graph_inspected "
            "is always false."
        ),
    },
    {
        "code": "observation_window_only",
        "kind": "notice",
        "message": (
            "Two complete payload observations plus identity and listing "
            "rechecks detect differences visible across those reads. "
            "Changes after the final observation are not covered."
        ),
    },
    {
        "code": "cli_only_not_mcp",
        "kind": "notice",
        "message": (
            "snapshot-export-verify is CLI-only and intentionally absent "
            "from the fixed 14-tool MCP set."
        ),
    },
)


class SnapshotExportVerifyError(Exception):
    """Malformed arguments or unsupported invocation. Default exit 2."""

    exit_code = 2


class SnapshotExportVerifyIntegrityError(SnapshotExportVerifyError):
    """Unsafe structure, invalid envelope, or concurrent change. Exit 1."""

    exit_code = 1


def parse_expected_export_revision(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotExportVerifyError(
            "expected-export-revision must be sha256:<64 lowercase hex>"
        )
    if value != value.strip():
        raise SnapshotExportVerifyError(
            "expected-export-revision must not contain leading or trailing "
            f"whitespace: {value!r}"
        )
    if not _REVISION_RE.fullmatch(value):
        raise SnapshotExportVerifyError(
            "expected-export-revision must be sha256:<64 lowercase hex>, "
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
    return (
        "snapshot-export-verify: "
        f"export_directory={result.get('export_directory')} "
        f"resolved={result.get('resolved_snapshot')} "
        f"expected_export_revision={result.get('expected_export_revision')} "
        f"observed_export_revision={result.get('observed_export_revision')} "
        f"files={result.get('file_count')} "
        f"total_size_bytes={result.get('total_size_bytes')} "
        f"revision_matches={str(bool(result.get('revision_matches'))).lower()} "
        f"payload_verified={str(bool(result.get('payload_verified'))).lower()} "
        f"ok={str(bool(result.get('ok'))).lower()} "
        "This verification is not a backup and is not authorization to delete anything."
    )


def _after_export_verify_directory_opened(
    _export_dir: Path, _directory_fd: int, _identity: Tuple[int, int, int, int]
) -> None:
    """Test hook after the export directory is anchored."""
    return None


def _after_export_verify_path_inspected(_export_dir: Path) -> None:
    """Test hook after initial path validation and before descriptor open."""
    return None


def _after_export_verify_listed(
    _export_dir: Path, _entries: Mapping[str, os.stat_result]
) -> None:
    """Test hook after the first anchored listing."""
    return None


def _after_export_verify_first_observation(
    _export_dir: Path, _records: Sequence[Mapping[str, Any]]
) -> None:
    """Test hook after the first complete observation and before the second."""
    return None


def _after_export_verify_second_listed(
    _export_dir: Path, _entries: Mapping[str, os.stat_result]
) -> None:
    """Test hook after the second-pass listing and before the second hashes."""
    return None


def _after_export_verify_result_ready(
    _export_dir: Path, _directory_fd: int, _result: Mapping[str, Any]
) -> None:
    """Test hook after the result is built and before it is yielded."""
    return None


def _structure_error(error: Exception) -> SnapshotExportVerifyError:
    message = str(error)
    if isinstance(error, SnapshotExportPlanIntegrityError):
        return SnapshotExportVerifyIntegrityError(message)
    if isinstance(error, SnapshotExportPlanError):
        return SnapshotExportVerifyIntegrityError(message)
    return SnapshotExportVerifyIntegrityError(message)


def _resolve_export_dir(export_dir: Path) -> Path:
    path = Path(export_dir)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotExportVerifyError(
            f"export directory does not exist: {path}"
        ) from error
    except OSError as error:
        raise SnapshotExportVerifyError(
            f"cannot inspect export directory {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotExportVerifyError(
            f"export directory must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotExportVerifyError(
            f"export directory is not a real directory: {path}"
        )
    _after_export_verify_path_inspected(path)
    # Canonicalization must happen only after this exact pathname has been
    # opened and anchored. Resolving here would follow a replacement symlink
    # introduced after lstat and redirect every later descriptor read.
    return path


def _listing_token(
    present: Mapping[str, os.stat_result],
) -> Dict[str, Tuple[int, int, int, int, int]]:
    return {name: _file_identity(info) for name, info in present.items()}


def _observe_directory(
    export_dir: Path,
    directory_fd: int,
    expected_identity: Tuple[int, int, int, int],
) -> Tuple[int, int, int, int]:
    try:
        opened = os.fstat(directory_fd)
    except OSError as error:
        raise SnapshotExportVerifyIntegrityError(
            f"cannot inspect export directory descriptor {export_dir}: {error}"
        ) from error
    opened_identity = _directory_identity(opened)
    if (
        not stat.S_ISDIR(opened.st_mode)
        or opened_identity != expected_identity
    ):
        raise SnapshotExportVerifyIntegrityError(
            "export directory descriptor changed during verification"
        )
    try:
        current = export_dir.lstat()
    except OSError as error:
        raise SnapshotExportVerifyIntegrityError(
            f"export directory changed during verification: {export_dir}"
        ) from error
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or _directory_identity(current) != expected_identity
    ):
        raise SnapshotExportVerifyIntegrityError(
            f"export directory changed or was replaced: {export_dir}"
        )
    return opened_identity


def _observe_listing(
    export_dir: Path,
    directory_fd: int,
    expected_identity: Tuple[int, int, int, int],
) -> Dict[str, os.stat_result]:
    try:
        return _payload_children(export_dir, directory_fd, expected_identity)
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _structure_error(error) from error


def _observe_payloads(
    export_dir: Path,
    directory_fd: int,
    present: Mapping[str, os.stat_result],
) -> Tuple[str, List[Dict[str, Any]], Dict[str, Tuple[int, int, int, int, int]], Dict[str, str]]:
    if MANIFEST_NAME not in present:
        raise SnapshotExportVerifyIntegrityError(
            "export directory is missing required payload manifest.json"
        )
    try:
        manifest, manifest_revision, manifest_identity = _load_manifest(
            directory_fd, export_dir / MANIFEST_NAME
        )
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _structure_error(error) from error
    if manifest_identity != _file_identity(present[MANIFEST_NAME]):
        raise SnapshotExportVerifyIntegrityError(
            "manifest changed after the anchored payload listing"
        )
    resolved = manifest.get("id")
    if not isinstance(resolved, str):
        raise SnapshotExportVerifyIntegrityError(
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
        path = export_dir / name
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
            raise SnapshotExportVerifyIntegrityError(
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


def _build_result(
    export_dir: Path,
    expected: str,
    resolved: str,
    records: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    files = [dict(item) for item in records]
    result: Dict[str, Any] = {
        "schema_version": VERIFY_SCHEMA_VERSION,
        "ok": False,
        "export_directory": str(export_dir),
        "resolved_snapshot": resolved,
        "files": files,
        "file_count": len(files),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in files),
        "expected_export_revision": expected,
        "observed_export_revision": export_revision_of(
            {
                "schema_version": PLAN_SCHEMA_VERSION,
                "resolved_snapshot": resolved,
                "files": files,
            }
        ),
        "revision_matches": False,
        "payload_verified": True,
        "export_mutated": False,
        "graph_inspected": False,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }
    matches = result["observed_export_revision"] == expected
    result["revision_matches"] = matches
    result["ok"] = matches
    return result


@contextmanager
def _snapshot_export_verify_scope(
    export_dir: Path, expected_export_revision: object
) -> Iterator[Dict[str, Any]]:
    """Yield one verify result while the export directory descriptor is held."""
    expected = parse_expected_export_revision(expected_export_revision)
    root = _resolve_export_dir(export_dir)
    try:
        _require_descriptor_reads()
    except SnapshotExportPlanError as error:
        raise SnapshotExportVerifyError(str(error)) from error
    try:
        directory_fd, opened_identity = _open_directory_nofollow(
            root, label="export directory"
        )
    except (SnapshotExportPlanError, SnapshotExportPlanIntegrityError) as error:
        raise _structure_error(error) from error
    try:
        _observe_directory(root, directory_fd, opened_identity)
        try:
            canonical_root = root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise SnapshotExportVerifyIntegrityError(
                f"export directory changed during canonicalization: {root}"
            ) from error
        # The canonical report path must name the inode already held by the
        # descriptor. This comparison prevents resolve() from redirecting the
        # anchor if the caller-supplied pathname changes concurrently.
        _observe_directory(canonical_root, directory_fd, opened_identity)
        root = canonical_root
        _after_export_verify_directory_opened(root, directory_fd, opened_identity)
        first_dir = _observe_directory(root, directory_fd, opened_identity)
        first_present = _observe_listing(root, directory_fd, opened_identity)
        _after_export_verify_listed(root, first_present)
        first_resolved, first_records, first_ids, first_revs = _observe_payloads(
            root, directory_fd, first_present
        )
        _after_export_verify_first_observation(root, first_records)

        second_dir = _observe_directory(root, directory_fd, opened_identity)
        second_present = _observe_listing(root, directory_fd, opened_identity)
        _after_export_verify_second_listed(root, second_present)
        second_resolved, second_records, second_ids, second_revs = _observe_payloads(
            root, directory_fd, second_present
        )
        final_dir = _observe_directory(root, directory_fd, opened_identity)
        final_present = _observe_listing(root, directory_fd, opened_identity)

        if (
            first_dir != opened_identity
            or second_dir != opened_identity
            or final_dir != opened_identity
            or _listing_token(first_present) != _listing_token(second_present)
            or _listing_token(first_present) != _listing_token(final_present)
            or first_resolved != second_resolved
            or first_records != second_records
            or first_ids != second_ids
            or first_revs != second_revs
        ):
            raise SnapshotExportVerifyIntegrityError(
                "export directory listing, manifest, or payload changed "
                "during verification"
            )
        result = _build_result(root, expected, first_resolved, first_records)
        _after_export_verify_result_ready(root, directory_fd, result)
        yield result
    finally:
        os.close(directory_fd)


def snapshot_export_verify(
    export_dir: Path, expected_export_revision: str
) -> Dict[str, Any]:
    """Verify one standalone export directory without writing files or streams."""
    with _snapshot_export_verify_scope(export_dir, expected_export_revision) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that one standalone export directory still contains "
            "exactly the snapshot envelope bound by an expected "
            "export_revision. Checks bytes and structure only. Does not "
            "inspect a graph or mutate the export. This verification is "
            "not a backup and is not authorization to delete anything. "
            "Not an MCP tool."
        )
    )
    parser.add_argument(
        "--export-dir",
        type=Path,
        required=True,
        help="Standalone export directory, relative to cwd.",
    )
    parser.add_argument(
        "--expected-export-revision",
        required=True,
        help="sha256:<64 lowercase hex> from snapshot-export-plan",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_export_verify_scope(
            args.export_dir, args.expected_export_revision
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            sys.stdout.flush()
            return 0 if result["ok"] else 1
    except SnapshotExportVerifyError as error:
        print(f"snapshot-export-verify: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-export-verify: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
