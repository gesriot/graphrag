#!/usr/bin/env python
"""Operator-triggered snapshot prune guarded by a retention-plan revision.

``snapshot-prune`` applies exactly one CAS-verified
``snapshot-retention-plan``: it deletes the recomputed plan's
``deletion_candidates`` and nothing else. There is no dry-run mode; the
existing plan command is the preview. Without ``--prune-confirmed`` the
command exits 2 and changes nothing.

The command requires an already-adopted regular ``.publish.lock`` and
never creates, truncates, rewrites, chmods, or replaces that lock. It
never creates ``.snapshot-pins.json``. One exclusive existing-lock lease
covers validation, plan recomputation, CAS, deletion, result
construction, serialization, stdout write, and stdout flush. It does
not take a nested shared lease and does not call the unguarded public
cleanup helper. Advisory locks protect only cooperating
processes. MCP stays exactly 11 read-only tools; this command is
CLI-only.

Recursive deletion of several directories is not transactionally
atomic. A crash or later-candidate failure can leave a partially
applied prune. There is no rollback, trash, or recovery protocol.

Usage:
    graphrag-code snapshot-prune --graph <root> --keep-last <N> \\
        --expected-plan-revision sha256:<hex> --prune-confirmed [--json]
    python -m graphrag_code.snapshot_prune --graph <root> --keep-last <N> \\
        --expected-plan-revision sha256:<hex> --prune-confirmed [--json]
    uv run python scripts/snapshot_prune.py --graph <root> --keep-last <N> \\
        --expected-plan-revision sha256:<hex> --prune-confirmed [--json]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence

from graphrag_code.byog_graph import (
    ByogPublicationLockError,
    ByogReaderLockError,
    _validate_managed_snapshot_layout,
    graph_exclusive_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
)
from graphrag_code.snapshot_retention import (
    SnapshotRetentionError,
    SnapshotRetentionIntegrityError,
    _build_plan_unlocked,
    _parse_keep_last,
)

PRUNE_SCHEMA_VERSION = 1
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_ERROR_CHARS = 400
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_CONFIRMATION_MESSAGE = """\
refusing to prune snapshots without --prune-confirmed.

This is an explicit mutating CLI operation. It deletes only the
CAS-verified deletion-candidate directories under <graph>/snapshots/.
It does not activate, publish, change current, write the pin registry,
repair, or reindex. snapshot-retention-plan is the preview; this
command has no dry-run mode.

--expected-plan-revision is a compare-and-swap guard: the exclusive
existing-lock lease recomputes the complete retention plan and deletes
only when that token still matches. Recursive deletion is not
transactionally atomic. A crash or later-candidate failure can leave a
partially applied prune; there is no rollback. Advisory locks protect
only cooperating processes.
""".strip()


class SnapshotPruneError(Exception):
    """Expected prune failure before deletion. Default exit 2."""

    exit_code = 2


class SnapshotPruneIntegrityError(SnapshotPruneError):
    """Stale plan revision, integrity, or concurrency. Exit 1."""

    exit_code = 1


def parse_plan_revision(value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotPruneError(
            "expected-plan-revision must be sha256:<64 lowercase hex>"
        )
    if value != value.strip():
        raise SnapshotPruneError(
            "expected-plan-revision must not contain leading or trailing "
            f"whitespace: {value!r}"
        )
    if not _REVISION_RE.fullmatch(value):
        raise SnapshotPruneError(
            "expected-plan-revision must be sha256:<64 lowercase hex>, "
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
    if result.get("partial"):
        return (
            "snapshot-prune: PARTIAL FAILURE "
            f"graph={result.get('graph')} "
            f"deleted={result.get('deleted_count')} "
            f"failed={result.get('failed_snapshot')} "
            f"not_attempted={len(result.get('not_attempted_snapshots') or [])} "
            f"remaining={len(result.get('remaining_published_snapshots') or [])} "
            "filesystem_may_have_changed=true "
            "retry_requires_fresh_plan=true "
            "There is no rollback; capture a fresh snapshot-retention-plan "
            "before retry."
        )
    return (
        "snapshot-prune: "
        f"graph={result.get('graph')} "
        f"current={result.get('current')} "
        f"keep_last_requested={result.get('keep_last_requested')} "
        f"keep_last_effective={result.get('keep_last_effective')} "
        f"deleted={result.get('deleted_count')} "
        f"remaining={len(result.get('remaining_published_snapshots') or [])} "
        f"observed_plan_revision={result.get('observed_plan_revision')} "
        f"changed={str(result.get('changed')).lower()} "
        "filesystem_may_have_changed="
        f"{str(result.get('filesystem_may_have_changed')).lower()}"
    )


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotPruneError(f"graph root does not exist: {path}") from error
    except OSError as error:
        raise SnapshotPruneError(
            f"cannot inspect graph root {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotPruneError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotPruneError(f"graph root is not a real directory: {path}")
    return path.resolve()


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotPruneError(str(error)) from error
    if not managed:
        raise SnapshotPruneError(
            "legacy flat-parquet directory has no managed snapshot history "
            f"to prune: {root}"
        )


def _lock_error(error: Exception) -> SnapshotPruneError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotPruneError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotPruneIntegrityError(message)
    return SnapshotPruneError(message)


def _bounded_error(error: BaseException) -> str:
    text = f"{type(error).__name__}: {error}"
    text = " ".join(text.split())
    if len(text) > _MAX_ERROR_CHARS:
        return text[: _MAX_ERROR_CHARS - 3] + "..."
    return text


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(dict.fromkeys(values), key=lambda item: item.encode("utf-8"))


def _assert_real_snapshots_dir(snapshots_dir: Path) -> None:
    try:
        info = snapshots_dir.lstat()
    except FileNotFoundError as error:
        raise SnapshotPruneError(
            f"snapshots directory missing: {snapshots_dir}"
        ) from error
    except OSError as error:
        raise SnapshotPruneError(
            f"cannot inspect snapshots directory {snapshots_dir}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotPruneError(
            f"unsafe symlinked snapshots directory: {snapshots_dir}"
        )
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotPruneError(
            f"snapshots path is not a real directory: {snapshots_dir}"
        )


def _assert_direct_published_directory(snapshots_dir: Path, snap_id: str) -> Path:
    if not isinstance(snap_id, str) or not is_published_snapshot_id(snap_id):
        raise SnapshotPruneError(
            f"deletion candidate is not a canonical published id: {snap_id!r}"
        )
    if is_staging_snapshot_name(snap_id):
        raise SnapshotPruneError(
            f"staging path is not a published snapshot: {snap_id!r}"
        )
    target = snapshots_dir / snap_id
    if target.name != snap_id or target.parent != snapshots_dir:
        raise SnapshotPruneError(f"unsafe snapshot path: {snap_id!r}")
    try:
        info = target.lstat()
    except FileNotFoundError as error:
        raise SnapshotPruneIntegrityError(
            f"deletion candidate is not a published snapshots directory: {target}"
        ) from error
    except OSError as error:
        raise SnapshotPruneIntegrityError(
            f"cannot inspect deletion candidate {target}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotPruneError(f"unsafe symlinked snapshot entry: {target}")
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotPruneError(
            f"deletion candidate is not a real directory: {target}"
        )
    return target


def _remove_published_snapshot_directory(snapshots_dir: Path, snap_id: str) -> None:
    """Delete one CAS-verified published snapshot directory.

    Tests monkeypatch this primitive instead of changing host permissions.
    """
    _assert_real_snapshots_dir(snapshots_dir)
    target = _assert_direct_published_directory(snapshots_dir, snap_id)
    shutil.rmtree(target)
    if _lexists(target):
        raise SnapshotPruneIntegrityError(
            f"deletion candidate is still present after removal: {target}"
        )


def _plan_or_raise(root: Path, keep_last: int) -> Dict[str, Any]:
    try:
        return _build_plan_unlocked(root, keep_last)
    except SnapshotRetentionIntegrityError as error:
        raise SnapshotPruneIntegrityError(str(error)) from error
    except SnapshotRetentionError as error:
        raise SnapshotPruneError(str(error)) from error


def _require_matching_revision(plan: Mapping[str, Any], expected: str) -> None:
    observed = plan.get("plan_revision")
    if observed != expected:
        raise SnapshotPruneIntegrityError(
            f"expected-plan-revision {expected!r} does not match "
            f"observed {observed!r}; refusing to delete"
        )


def _validate_deletion_set(root: Path, plan: Mapping[str, Any]) -> List[str]:
    snapshots_dir = root / "snapshots"
    _assert_real_snapshots_dir(snapshots_dir)

    candidates = list(plan["deletion_candidates"])
    retained = list(plan["retained_snapshots"])
    published = list(plan["published_snapshots"])
    current = plan["current"]
    existing_operator = list(plan["existing_operator_pins"])
    existing_claim = list(plan["existing_claim_pins"])
    dangling = [
        *(plan.get("dangling_operator_pins") or []),
        *(plan.get("dangling_claim_pins") or []),
    ]

    if candidates != _byte_sort(candidates):
        raise SnapshotPruneIntegrityError(
            "deletion candidates are not in canonical UTF-8-byte order"
        )
    if set(candidates) & set(retained):
        raise SnapshotPruneIntegrityError(
            "deletion candidates overlap retained snapshots"
        )
    if current in candidates:
        raise SnapshotPruneIntegrityError(
            f"current snapshot is a deletion candidate: {current!r}"
        )
    if set(candidates) & set(existing_operator):
        raise SnapshotPruneIntegrityError(
            "deletion candidates overlap existing operator pins"
        )
    if set(candidates) & set(existing_claim):
        raise SnapshotPruneIntegrityError(
            "deletion candidates overlap existing claim pins"
        )
    if set(candidates) & set(dangling):
        raise SnapshotPruneIntegrityError(
            "dangling pins must not be treated as deletion paths"
        )
    published_set = set(published)
    for snap_id in candidates:
        if snap_id not in published_set:
            raise SnapshotPruneIntegrityError(
                f"deletion candidate is not a published snapshot: {snap_id!r}"
            )
        if is_staging_snapshot_name(snap_id):
            raise SnapshotPruneError(
                f"staging path is not a published snapshot: {snap_id!r}"
            )
        _assert_direct_published_directory(snapshots_dir, snap_id)
    if current is not None:
        _assert_direct_published_directory(snapshots_dir, current)
    for snap_id in retained:
        _assert_direct_published_directory(snapshots_dir, snap_id)
    return candidates


def _remaining_after(
    plan: Mapping[str, Any],
    *,
    deleted: Sequence[str],
    failed: Optional[str],
    not_attempted: Sequence[str],
) -> List[str]:
    remaining = [
        snap_id
        for snap_id in plan["published_snapshots"]
        if snap_id not in set(deleted)
    ]
    if failed is not None and failed not in remaining:
        remaining.append(failed)
    for snap_id in not_attempted:
        if snap_id not in remaining:
            remaining.append(snap_id)
    return [snap_id for snap_id in plan["published_snapshots"] if snap_id in set(remaining)]


def _result(
    plan: Mapping[str, Any],
    *,
    expected: str,
    deleted: Sequence[str],
    failed_snapshot: Optional[str],
    not_attempted: Sequence[str],
    error: Optional[str],
    partial: bool,
) -> Dict[str, Any]:
    deleted_ids = list(deleted)
    remaining = _remaining_after(
        plan,
        deleted=deleted_ids,
        failed=failed_snapshot,
        not_attempted=not_attempted,
    )
    ok = not partial
    return {
        "schema_version": PRUNE_SCHEMA_VERSION,
        "ok": ok,
        # ``changed`` counts candidates whose complete directory removal
        # returned successfully.  A failing rmtree may nevertheless have
        # removed some children, so partial execution needs a separate,
        # deliberately conservative signal.
        "changed": bool(deleted_ids),
        "filesystem_may_have_changed": bool(deleted_ids) or partial,
        "partial": partial,
        "graph": plan["graph"],
        "keep_last_requested": plan["keep_last_requested"],
        "keep_last_effective": plan["keep_last_effective"],
        "expected_plan_revision": expected,
        "observed_plan_revision": plan["plan_revision"],
        "current": plan["current"],
        "registry_revision": plan["registry_revision"],
        "retained_snapshots": list(plan["retained_snapshots"]),
        "planned_deletion_candidates": list(plan["deletion_candidates"]),
        "deleted_snapshots": deleted_ids,
        "deleted_count": len(deleted_ids),
        "failed_snapshot": failed_snapshot,
        "not_attempted_snapshots": list(not_attempted),
        "remaining_published_snapshots": remaining,
        "retry_requires_fresh_plan": partial,
        "error": error,
    }


def _prune_unlocked(
    root: Path, keep_last: int, expected: str
) -> Dict[str, Any]:
    """Apply one CAS-verified plan. Caller must hold ``graph_exclusive_lease``."""
    _require_managed_graph(root)
    plan = _plan_or_raise(root, keep_last)
    _require_matching_revision(plan, expected)
    candidates = _validate_deletion_set(root, plan)
    recheck = _plan_or_raise(root, keep_last)
    _require_matching_revision(recheck, expected)
    if recheck["plan_revision"] != plan["plan_revision"]:
        raise SnapshotPruneIntegrityError(
            "retention plan changed before deletion; refusing to delete"
        )
    if list(recheck["deletion_candidates"]) != candidates:
        raise SnapshotPruneIntegrityError(
            "deletion candidates changed before deletion; refusing to delete"
        )
    if not candidates:
        return _result(
            plan,
            expected=expected,
            deleted=[],
            failed_snapshot=None,
            not_attempted=[],
            error=None,
            partial=False,
        )

    snapshots_dir = root / "snapshots"
    deleted: List[str] = []
    for index, snap_id in enumerate(candidates):
        try:
            _remove_published_snapshot_directory(snapshots_dir, snap_id)
        except Exception as error:
            return _result(
                plan,
                expected=expected,
                deleted=deleted,
                failed_snapshot=snap_id,
                not_attempted=candidates[index + 1 :],
                error=_bounded_error(error),
                partial=True,
            )
        deleted.append(snap_id)
    return _result(
        plan,
        expected=expected,
        deleted=deleted,
        failed_snapshot=None,
        not_attempted=[],
        error=None,
        partial=False,
    )


@contextmanager
def _snapshot_prune_scope(
    graph: Path,
    keep_last: object,
    expected_plan_revision: object,
    *,
    prune_confirmed: bool,
) -> Iterator[Dict[str, Any]]:
    """Yield one prune result while its exclusive lease remains held."""
    if not prune_confirmed:
        raise SnapshotPruneError(_CONFIRMATION_MESSAGE)
    try:
        requested = _parse_keep_last(keep_last)
    except SnapshotRetentionError as error:
        raise SnapshotPruneError(str(error)) from error
    expected = parse_plan_revision(expected_plan_revision)
    root = _resolve_graph_root(graph)
    _require_managed_graph(root)
    try:
        with graph_exclusive_lease(root):
            yield _prune_unlocked(root, requested, expected)
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_prune(
    graph: Path,
    keep_last: int,
    expected_plan_revision: str,
    *,
    prune_confirmed: bool,
) -> Dict[str, Any]:
    """Apply one CAS-verified retention plan.

    Returns the stable result object. Complete success has ``ok=true``.
    Partial mutation has ``ok=false`` and ``partial=true`` and does not
    raise; the CLI still exits 1 after emitting that result. Pre-deletion
    failures raise :class:`SnapshotPruneError` (exit 2) or
    :class:`SnapshotPruneIntegrityError` (exit 1).
    """
    with _snapshot_prune_scope(
        graph,
        keep_last,
        expected_plan_revision,
        prune_confirmed=prune_confirmed,
    ) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Delete the CAS-verified snapshot-retention-plan deletion "
            "candidates. Requires --prune-confirmed and "
            "--expected-plan-revision. snapshot-retention-plan is the "
            "preview; this command has no dry-run. Never creates "
            ".publish.lock, and is not an MCP tool. Recursive deletion "
            "is not transactionally atomic."
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
        "--expected-plan-revision",
        required=True,
        help="sha256:<64 lowercase hex> from snapshot-retention-plan",
    )
    parser.add_argument(
        "--prune-confirmed",
        action="store_true",
        help=(
            "Required to delete CAS-verified deletion-candidate "
            "directories. The command still refuses to delete if the "
            "recomputed plan_revision no longer matches."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_prune_scope(
            args.graph,
            args.keep_last,
            args.expected_plan_revision,
            prune_confirmed=bool(args.prune_confirmed),
        ) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing the graph lease so
            # the complete response is handed to the caller under that lease.
            sys.stdout.flush()
            if result.get("partial") or not result.get("ok"):
                return 1
    except SnapshotPruneError as error:
        print(f"snapshot-prune: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-prune: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
