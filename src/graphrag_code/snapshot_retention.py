#!/usr/bin/env python
"""Read-only snapshot retention plan.

``snapshot-retention-plan`` reports exactly which published snapshots
cooperating keep-last cleanup would retain and delete. It shares
``plan_snapshot_retention`` with cleanup. This command does not prune,
apply, delete, activate, publish, or write any graph file.

An absent ``.snapshot-pins.json`` is an empty operator pin set and is
not created. The command requires an already-adopted regular
``.publish.lock`` and never creates, truncates, rewrites, chmods, or
replaces that lock. Missing lock exits 2 and points at
``adopt-publication-lock``. Advisory locks protect only cooperating
processes. Common lock-ignoring changes to the decision inputs during
discovery are detected by a second read and exit 1. MCP stays exactly
13 read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-retention-plan --graph <root> --keep-last <N> [--json]
    python -m graphrag_code.snapshot_retention --graph <root> --keep-last <N> [--json]
    uv run python scripts/snapshot_retention.py --graph <root> --keep-last <N> [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional

from graphrag_code.byog_graph import (
    PUBLICATION_LOCK_NAME,
    ByogPublicationLockError,
    ByogReaderLockError,
    _read_current_id_for_retention,
    _validate_managed_snapshot_layout,
    graph_read_lease,
    plan_snapshot_retention,
)
from graphrag_code.snapshot_pins import (
    ABSENT_REVISION,
    MAX_REGISTRY_BYTES,
    OPERATOR_PINS_NAME,
    SnapshotPinsError,
    load_operator_pins_unlocked,
    revision_of_bytes,
)

PLAN_SCHEMA_VERSION = 1
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_PLAN_REVISION_KEYS = (
    "claim_pins",
    "current",
    "deletion_candidates",
    "keep_last_effective",
    "operator_pins",
    "published_snapshots",
    "registry_revision",
    "retained_snapshots",
    "schema_version",
)


class SnapshotRetentionError(Exception):
    """Expected retention-plan failure. Default exit 2."""

    exit_code = 2


class SnapshotRetentionIntegrityError(SnapshotRetentionError):
    """Persisted integrity or concurrent mutation. Exit 1."""

    exit_code = 1


def _byte_sort(values: List[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def _parse_keep_last(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotRetentionError("keep-last must be an integer")
    return value


def canonical_plan_revision_payload(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Decision inputs bound by ``plan_revision``. Presentation fields excluded."""
    payload: Dict[str, Any] = {}
    for key in _PLAN_REVISION_KEYS:
        if key not in result:
            raise SnapshotRetentionError(
                f"retention plan is missing decision input {key!r}"
            )
        payload[key] = result[key]
    return payload


def canonical_plan_revision_text(result: Mapping[str, Any]) -> str:
    """Canonical JSON of the decision inputs. Documented hash input.

    Compact UTF-8 JSON with sorted keys, no trailing newline:
    ``json.dumps(..., sort_keys=True, separators=(",", ":"),
    ensure_ascii=True, allow_nan=False)``.
    """
    return json.dumps(
        canonical_plan_revision_payload(result),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def plan_revision_of(result: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        canonical_plan_revision_text(result).encode("utf-8")
    ).hexdigest()
    return "sha256:" + digest


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
        "snapshot-retention-plan: "
        f"graph={result.get('graph')} "
        f"current={result.get('current')} "
        f"keep_last_requested={result.get('keep_last_requested')} "
        f"keep_last_effective={result.get('keep_last_effective')} "
        f"published={result.get('published_count')} "
        f"retained={len(result.get('retained_snapshots') or [])} "
        f"deletion_candidates={len(result.get('deletion_candidates') or [])} "
        f"registry_revision={result.get('registry_revision')} "
        f"plan_revision={result.get('plan_revision')}"
    )


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotRetentionError(f"graph root does not exist: {path}") from error
    except OSError as error:
        raise SnapshotRetentionError(
            f"cannot inspect graph root {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotRetentionError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotRetentionError(f"graph root is not a real directory: {path}")
    return path.resolve()


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotRetentionError(str(error)) from error
    if not managed:
        raise SnapshotRetentionError(
            "legacy flat-parquet directory has no managed snapshot history "
            f"to plan: {root}"
        )


def _lock_error(error: Exception) -> SnapshotRetentionError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotRetentionError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotRetentionIntegrityError(message)
    return SnapshotRetentionError(message)


def _lock_identity(root: Path) -> tuple[int, int, int, int]:
    lock = root / PUBLICATION_LOCK_NAME
    try:
        info = lock.lstat()
    except FileNotFoundError as error:
        raise SnapshotRetentionIntegrityError(
            f"publication lock disappeared during retention plan: {lock}"
        ) from error
    except OSError as error:
        raise SnapshotRetentionIntegrityError(
            f"cannot inspect publication lock {lock}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotRetentionIntegrityError(
            f"unsafe symlinked publication lock is unsupported: {lock}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotRetentionIntegrityError(
            f"publication lock is not a regular file: {lock}"
        )
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _registry_revision_now(root: Path) -> str:
    """Re-read exact registry bytes without parsing the registry a second time."""
    path = root / OPERATOR_PINS_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ABSENT_REVISION
    except OSError as error:
        raise SnapshotRetentionIntegrityError(
            f"cannot re-inspect operator pin registry {path}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SnapshotRetentionIntegrityError(
            f"operator pin registry changed during retention plan: {path}"
        )
    if info.st_size > MAX_REGISTRY_BYTES:
        raise SnapshotRetentionIntegrityError(
            f"operator pin registry changed during retention plan: {path}"
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(path), flags)
    except OSError as error:
        raise SnapshotRetentionIntegrityError(
            f"cannot safely re-open operator pin registry {path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != info.st_dev
            or opened.st_ino != info.st_ino
        ):
            raise SnapshotRetentionIntegrityError(
                f"operator pin registry changed during retention plan: {path}"
            )
        chunks: List[bytes] = []
        total = 0
        while total <= MAX_REGISTRY_BYTES:
            chunk = os.read(fd, min(8192, MAX_REGISTRY_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        try:
            current = path.lstat()
        except OSError as error:
            raise SnapshotRetentionIntegrityError(
                f"operator pin registry changed during retention plan: {path}"
            ) from error
        if current.st_dev != opened.st_dev or current.st_ino != opened.st_ino:
            raise SnapshotRetentionIntegrityError(
                f"operator pin registry changed during retention plan: {path}"
            )
    finally:
        os.close(fd)
    if len(data) > MAX_REGISTRY_BYTES:
        raise SnapshotRetentionIntegrityError(
            f"operator pin registry changed during retention plan: {path}"
        )
    return revision_of_bytes(data)


def _wrap_compare(error: Exception) -> SnapshotRetentionError:
    from graphrag_code.snapshot_compare import SnapshotCompareIntegrityError

    if isinstance(error, SnapshotCompareIntegrityError):
        return SnapshotRetentionIntegrityError(str(error))
    return SnapshotRetentionError(str(error))


def _resolve_current_once(root: Path) -> str:
    from graphrag_code.snapshot_compare import (
        SnapshotCompareError,
        _resolve_current_once as resolve_current,
    )

    try:
        _snap_dir, current_id, _manifest = resolve_current(root)
    except SnapshotCompareError as error:
        raise _wrap_compare(error) from error
    return current_id


def _published_and_notices(root: Path) -> tuple[List[str], List[Dict[str, Any]]]:
    from graphrag_code.snapshot_compare import (
        SnapshotCompareError,
        _list_published_and_notices,
    )

    try:
        published, notices = _list_published_and_notices(root)
    except SnapshotCompareError as error:
        raise _wrap_compare(error) from error
    return list(published), list(notices)


def _claim_pins(root: Path) -> List[str]:
    from graphrag_code.byog_graph import pinned_snapshot_ids

    return _byte_sort(sorted(pinned_snapshot_ids(root)))


def _verify_decision_inputs_unchanged(
    root: Path,
    *,
    lock_identity: tuple[int, int, int, int],
    current_id: str,
    registry_revision: str,
    claim_pins: List[str],
    published: List[str],
    notices: List[Dict[str, Any]],
) -> None:
    """Reject lock-ignoring mutation during discovery of one plan."""
    try:
        checked_current = _read_current_id_for_retention(root)
        checked_published, checked_notices = _published_and_notices(root)
        checked_claims = _claim_pins(root)
        checked_registry_revision = _registry_revision_now(root)
        checked_lock = _lock_identity(root)
    except SnapshotRetentionIntegrityError:
        raise
    except Exception as error:
        raise SnapshotRetentionIntegrityError(
            f"retention decision inputs changed during the read: {error}"
        ) from error
    changed: List[str] = []
    if checked_current != current_id:
        changed.append("current")
    if checked_published != published:
        changed.append("published_snapshots")
    if checked_notices != notices:
        changed.append("staging_notices")
    if checked_claims != claim_pins:
        changed.append("claim_pins")
    if checked_registry_revision != registry_revision:
        changed.append("registry_revision")
    if checked_lock != lock_identity:
        changed.append("publication_lock")
    if changed:
        raise SnapshotRetentionIntegrityError(
            "retention decision inputs changed during the read: "
            + ", ".join(changed)
        )


def verify_retention_plan_inputs_unlocked(
    root: Path,
    plan: Mapping[str, Any],
    *,
    lock_identity: tuple[int, int, int, int],
    check_staging_notices: bool = True,
) -> None:
    """Revalidate one public retention plan while the caller holds a lease.

    The public plan stores ``published_snapshots`` in canonical byte order,
    whereas the planner's internal two-scan verifier compares the raw directory
    discovery order.  Apply paths must compare against the public CAS plan,
    not adopt a fresh raw-order scan as their baseline.

    Composite maintenance apply may disable only the staging-notice comparison
    after it has intentionally deleted the CAS-verified staging candidates.
    Those notices are presentation-only retention fields; current, published
    snapshots, both pin sources, the registry revision, and publication-lock
    identity are always checked.
    """
    try:
        checked_current = _read_current_id_for_retention(root)
        checked_published, checked_notices = _published_and_notices(root)
        checked_claims = _claim_pins(root)
        checked_registry_revision = _registry_revision_now(root)
        checked_lock = _lock_identity(root)
    except SnapshotRetentionIntegrityError:
        raise
    except Exception as error:
        raise SnapshotRetentionIntegrityError(
            f"retention decision inputs changed during apply: {error}"
        ) from error
    changed: List[str] = []
    if checked_current != plan.get("current"):
        changed.append("current")
    if _byte_sort(checked_published) != list(plan.get("published_snapshots") or []):
        changed.append("published_snapshots")
    if check_staging_notices and checked_notices != list(
        plan.get("staging_notices") or []
    ):
        changed.append("staging_notices")
    if checked_claims != list(plan.get("claim_pins") or []):
        changed.append("claim_pins")
    if checked_registry_revision != plan.get("registry_revision"):
        changed.append("registry_revision")
    if checked_lock != lock_identity:
        changed.append("publication_lock")
    if changed:
        raise SnapshotRetentionIntegrityError(
            "retention decision inputs changed during apply: " + ", ".join(changed)
        )


def build_stable_retention_plan_unlocked(
    root: Path, keep_last: int
) -> Dict[str, Any]:
    """One retention plan. Caller must already hold the graph lease."""
    _require_managed_graph(root)
    lock_identity = _lock_identity(root)

    current_id = _resolve_current_once(root)
    try:
        revision, operator = load_operator_pins_unlocked(root)
    except SnapshotPinsError as error:
        raise SnapshotRetentionError(
            f"operator pin registry is unsafe or malformed: {error}"
        ) from error
    claim = _claim_pins(root)
    published, notices = _published_and_notices(root)
    if current_id not in published:
        # _resolve_current_once proved the target immediately before discovery,
        # so its absence now is concurrent mutation rather than static input.
        raise SnapshotRetentionIntegrityError(
            "current snapshot disappeared from the published listing during "
            f"the read: {current_id!r}"
        )
    _verify_decision_inputs_unchanged(
        root,
        lock_identity=lock_identity,
        current_id=current_id,
        registry_revision=revision,
        claim_pins=claim,
        published=published,
        notices=notices,
    )
    try:
        planned = plan_snapshot_retention(
            keep_last=keep_last,
            current_id=current_id,
            published_ids=published,
            operator_pins=operator,
            claim_pins=claim,
        )
    except ValueError as error:
        raise SnapshotRetentionError(str(error)) from error
    result: Dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "graph": str(root),
        "keep_last_requested": keep_last,
        "keep_last_effective": planned["keep_last_effective"],
        "current": current_id,
        "registry_revision": revision,
        "published_count": len(planned["published_snapshots"]),
        "published_snapshots": planned["published_snapshots"],
        "operator_pins": planned["operator_pins"],
        "claim_pins": planned["claim_pins"],
        "effective_pins": planned["effective_pins"],
        "existing_operator_pins": planned["existing_operator_pins"],
        "existing_claim_pins": planned["existing_claim_pins"],
        "dangling_operator_pins": planned["dangling_operator_pins"],
        "dangling_claim_pins": planned["dangling_claim_pins"],
        "retained_snapshots": planned["retained_snapshots"],
        "deletion_candidates": planned["deletion_candidates"],
        "staging_notices": notices,
    }
    result["plan_revision"] = plan_revision_of(result)
    return result


def _build_plan_unlocked(root: Path, keep_last: int) -> Dict[str, Any]:
    return build_stable_retention_plan_unlocked(root, keep_last)


@contextmanager
def _snapshot_retention_plan_scope(
    graph: Path, keep_last: object
) -> Iterator[Dict[str, Any]]:
    """Yield one plan while its shared existing-lock lease remains held."""
    requested = _parse_keep_last(keep_last)
    root = _resolve_graph_root(graph)
    try:
        with graph_read_lease(root, allow_unlocked_managed=False):
            yield _build_plan_unlocked(root, requested)
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_retention_plan(graph: Path, keep_last: int) -> Dict[str, Any]:
    """Build one retention plan without writing files or process streams."""
    with _snapshot_retention_plan_scope(graph, keep_last) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report what cooperating keep-last cleanup would retain and "
            "delete. Read-only: does not prune, activate, or write files. "
            "Never creates .publish.lock or .snapshot-pins.json, and is "
            "not an MCP tool."
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
        help="Requested keep-last floor (effective minimum is 1).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_retention_plan_scope(args.graph, args.keep_last) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing the graph lease so the
            # complete response is handed to the caller under that lease.
            sys.stdout.flush()
    except SnapshotRetentionError as error:
        print(f"snapshot-retention-plan: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-retention-plan: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
