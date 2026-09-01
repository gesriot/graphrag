#!/usr/bin/env python
"""Read-only composite snapshot maintenance plan.

``snapshot-maintenance-plan`` embeds the current
``snapshot-retention-plan`` and the current schema-2
``snapshot-staging-cleanup-plan`` under one shared existing-lock graph
lease. It does not prune, clean staging, activate, publish, pin, repair,
or write any graph file. Existing ``snapshot-prune`` and
``snapshot-staging-cleanup`` remain the only apply commands.

Both embedded plans are computed inside the same shared lease. This
command does not take a nested graph-root lease. It never creates,
truncates, rewrites, chmods, or replaces ``.publish.lock``. It never
creates or changes ``.snapshot-pins.json``, ``current``, published
snapshots, staging directories, payload files, or writer-lock
bytes/metadata. MCP stays exactly 16 read-only tools; this command is
CLI-only.

``maintenance_revision`` is informational. No apply command accepts it.
Applying either component can invalidate the other revision. A fresh
composite or standalone plan is required after every apply.

Usage:
    graphrag-code snapshot-maintenance-plan --graph <root> --keep-last <N> [--json]
    python -m graphrag_code.snapshot_maintenance_plan --graph <root> --keep-last <N> [--json]
    uv run python scripts/snapshot_maintenance_plan.py --graph <root> --keep-last <N> [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from graphrag_code.byog_graph import (
    ByogPublicationLockError,
    ByogReaderLockError,
    graph_read_lease,
)
from graphrag_code.snapshot_retention import (
    SnapshotRetentionError,
    SnapshotRetentionIntegrityError,
    build_stable_retention_plan_unlocked,
)
from graphrag_code.snapshot_staging_cleanup_plan import (
    SnapshotStagingCleanupPlanError,
    SnapshotStagingCleanupPlanIntegrityError,
    build_stable_cleanup_plan_unlocked,
)

PLAN_SCHEMA_VERSION = 1
_COMPONENT_PRUNE = "snapshot-prune"
_COMPONENT_CLEANUP = "snapshot-staging-cleanup"
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_MAINTENANCE_REVISION_KEYS = (
    "actionable_components",
    "current",
    "fresh_plan_required_after_any_apply",
    "keep_last",
    "published_snapshots",
    "schema_version",
)
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "composite_is_read_only",
        "kind": "notice",
        "message": (
            "snapshot-maintenance-plan is a read-only composite of the "
            "current snapshot-retention-plan and schema-2 "
            "snapshot-staging-cleanup-plan. It does not prune, clean "
            "staging, pin, activate, publish, or write any graph file. "
            "snapshot-prune and snapshot-staging-cleanup remain the only "
            "apply commands."
        ),
    },
    {
        "code": "no_recommended_apply_order",
        "kind": "notice",
        "message": (
            "actionable_components lists which embedded plans currently "
            "have a non-empty deletion set. It does not recommend an "
            "apply order. Applying either component can invalidate the "
            "other revision."
        ),
    },
    {
        "code": "fresh_plan_required_after_any_apply",
        "kind": "notice",
        "message": (
            "A fresh composite or standalone plan is required after "
            "every apply before running another mutation. "
            "fresh_plan_required_after_any_apply is always true."
        ),
    },
    {
        "code": "maintenance_revision_informational",
        "kind": "notice",
        "message": (
            "maintenance_revision is informational only. No apply "
            "command accepts it. Capture a fresh plan after any apply "
            "and use the component plan_revision tokens with "
            "snapshot-prune or snapshot-staging-cleanup."
        ),
    },
    {
        "code": "advisory_locks_cooperating_only",
        "kind": "notice",
        "message": (
            "Advisory locks protect only cooperating processes. This "
            "shared existing-lock lease is not a liveness lease over a "
            "staging writer and does not infer writer death or "
            "ownership."
        ),
    },
)


class SnapshotMaintenancePlanError(Exception):
    """Expected maintenance-plan failure. Default exit 2."""

    exit_code = 2


class SnapshotMaintenancePlanIntegrityError(SnapshotMaintenancePlanError):
    """Persisted integrity, symlink, or concurrent mutation. Exit 1."""

    exit_code = 1


def _byte_sort_names(values: List[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def _parse_keep_last(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SnapshotMaintenancePlanError("keep-last must be a positive integer")
    if value < 1:
        raise SnapshotMaintenancePlanError("keep-last must be a positive integer")
    return value


def _wrap_component_error(error: Exception) -> SnapshotMaintenancePlanError:
    if isinstance(
        error,
        (SnapshotRetentionIntegrityError, SnapshotStagingCleanupPlanIntegrityError),
    ):
        return SnapshotMaintenancePlanIntegrityError(str(error))
    if isinstance(
        error, (SnapshotRetentionError, SnapshotStagingCleanupPlanError)
    ):
        wrapped = SnapshotMaintenancePlanError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    return SnapshotMaintenancePlanError(str(error))


def canonical_maintenance_revision_payload(
    result: Mapping[str, Any],
) -> Dict[str, Any]:
    """Decision inputs bound by ``maintenance_revision``."""
    payload: Dict[str, Any] = {}
    for key in _MAINTENANCE_REVISION_KEYS:
        if key not in result:
            raise SnapshotMaintenancePlanError(
                f"maintenance plan is missing decision input {key!r}"
            )
        payload[key] = result[key]
    try:
        payload["retention_plan"] = {
            "plan_revision": result["retention_plan"]["plan_revision"]
        }
        payload["staging_cleanup_plan"] = {
            "plan_revision": result["staging_cleanup_plan"]["plan_revision"]
        }
    except (KeyError, TypeError) as error:
        raise SnapshotMaintenancePlanError(
            "maintenance plan is missing an embedded plan_revision"
        ) from error
    return payload


def canonical_maintenance_revision_text(result: Mapping[str, Any]) -> str:
    """Canonical JSON of the composite decision inputs.

    Compact UTF-8 JSON with sorted keys, no trailing newline:
    ``json.dumps(..., sort_keys=True, separators=(",", ":"),
    ensure_ascii=True, allow_nan=False)``.

    Bound keys: ``schema_version``, ``keep_last``, ``current``,
    ``published_snapshots``, ``retention_plan.plan_revision``,
    ``staging_cleanup_plan.plan_revision``, ``actionable_components``,
    ``fresh_plan_required_after_any_apply``. Absolute graph path,
    counts, notices, ``ok``, and presentation-only embedded fields are
    excluded.
    """
    return json.dumps(
        canonical_maintenance_revision_payload(result),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def maintenance_revision_of(result: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(
        canonical_maintenance_revision_text(result).encode("utf-8")
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
    components = ",".join(result.get("actionable_components") or [])
    retention = result.get("retention_plan") or {}
    cleanup = result.get("staging_cleanup_plan") or {}
    suffix = ""
    if components:
        suffix += f" actionable={components}"
    return (
        "snapshot-maintenance-plan: "
        f"graph={result.get('graph')} "
        f"keep_last={result.get('keep_last')} "
        f"current={result.get('current')} "
        f"published={len(result.get('published_snapshots') or [])} "
        f"retention_plan_revision={retention.get('plan_revision')} "
        f"staging_cleanup_plan_revision={cleanup.get('plan_revision')} "
        f"maintenance_revision={result.get('maintenance_revision')} "
        "fresh_plan_required_after_any_apply=true"
        f"{suffix}"
    )


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotMaintenancePlanError(
            f"graph root does not exist: {path}"
        ) from error
    except OSError as error:
        raise SnapshotMaintenancePlanError(
            f"cannot inspect graph root {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotMaintenancePlanError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotMaintenancePlanError(
            f"graph root is not a real directory: {path}"
        )
    return path.resolve()


def _lock_error(error: Exception) -> SnapshotMaintenancePlanError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotMaintenancePlanError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotMaintenancePlanIntegrityError(message)
    return SnapshotMaintenancePlanError(message)


def _actionable_components(
    retention: Mapping[str, Any], cleanup: Mapping[str, Any]
) -> List[str]:
    names: List[str] = []
    if retention.get("deletion_candidates"):
        names.append(_COMPONENT_PRUNE)
    if cleanup.get("deletion_candidates"):
        names.append(_COMPONENT_CLEANUP)
    return _byte_sort_names(names)


def _after_retention_plan(_root: Path) -> None:
    """Test-only hook after the embedded retention plan is built."""
    return None


def build_stable_maintenance_plan_unlocked(
    root: Path, keep_last: int
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """One composite plan. Caller must already hold the graph lease.

    Returns ``(consistency, plan)``. ``consistency`` is the internal
    staging two-scan token used by apply revalidation. Do not expose it.
    """
    try:
        retention = build_stable_retention_plan_unlocked(root, keep_last)
    except SnapshotRetentionError as error:
        raise _wrap_component_error(error) from error
    _after_retention_plan(root)
    try:
        consistency, cleanup = build_stable_cleanup_plan_unlocked(root)
    except SnapshotStagingCleanupPlanError as error:
        raise _wrap_component_error(error) from error
    if retention.get("current") != cleanup.get("current"):
        raise SnapshotMaintenancePlanIntegrityError(
            "embedded retention and staging-cleanup plans disagree on current"
        )
    if list(retention.get("published_snapshots") or []) != list(
        cleanup.get("published_snapshots") or []
    ):
        raise SnapshotMaintenancePlanIntegrityError(
            "embedded retention and staging-cleanup plans disagree on "
            "published_snapshots"
        )
    result: Dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "ok": True,
        "graph": str(root),
        "keep_last": keep_last,
        "current": retention["current"],
        "published_snapshots": list(retention["published_snapshots"]),
        "retention_plan": retention,
        "staging_cleanup_plan": cleanup,
        "actionable_components": _actionable_components(retention, cleanup),
        "fresh_plan_required_after_any_apply": True,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }
    result["maintenance_revision"] = maintenance_revision_of(result)
    return consistency, result


def _build_plan_unlocked(root: Path, keep_last: int) -> Dict[str, Any]:
    _consistency, result = build_stable_maintenance_plan_unlocked(root, keep_last)
    return result


@contextmanager
def _snapshot_maintenance_plan_scope(
    graph: Path, keep_last: object
) -> Iterator[Dict[str, Any]]:
    """Yield one composite plan while its shared existing-lock lease remains held."""
    requested = _parse_keep_last(keep_last)
    root = _resolve_graph_root(graph)
    try:
        with graph_read_lease(root, allow_unlocked_managed=False):
            yield _build_plan_unlocked(root, requested)
    except SnapshotMaintenancePlanError:
        raise
    except SnapshotRetentionError as error:
        raise _wrap_component_error(error) from error
    except SnapshotStagingCleanupPlanError as error:
        raise _wrap_component_error(error) from error
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_maintenance_plan(graph: Path, keep_last: int) -> Dict[str, Any]:
    """Build one composite plan without writing files or process streams."""
    with _snapshot_maintenance_plan_scope(graph, keep_last) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report a read-only composite of the current "
            "snapshot-retention-plan and schema-2 "
            "snapshot-staging-cleanup-plan. Does not prune, clean "
            "staging, apply, or infer ownership. Never creates "
            ".publish.lock or .snapshot-pins.json, and is not an MCP "
            "tool. A fresh plan is required after any apply."
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
        help="Requested keep-last floor (positive integer).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_maintenance_plan_scope(args.graph, args.keep_last) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing the graph lease so the
            # complete response is handed to the caller under that lease.
            sys.stdout.flush()
    except SnapshotMaintenancePlanError as error:
        print(f"snapshot-maintenance-plan: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-maintenance-plan: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
