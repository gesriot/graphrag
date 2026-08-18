#!/usr/bin/env python
"""Read-only snapshot-export staging cleanup plan.

``snapshot-export-staging-cleanup-plan`` classifies every direct
``.graphrag-export-*`` child from the existing schema-1 inventory.
It does not delete, rename, quarantine, repair, claim, chmod,
truncate, create, or otherwise mutate any filesystem entry.
``deletion_candidates`` means only "selected by this read-only plan".
It is not ownership, cleanup eligibility, proof that apply created
the directory, proof that a writer died, or authorization to delete.
A writer may start after the plan is emitted.

This command reuses the snapshot-export-staging descriptor-relative,
no-follow, bounded two-scan observation scope. It does not invoke a
public CLI, does not perform a second path-based scan, and does not
inspect a managed graph. The parent descriptor plus retained
recognized staging and lock descriptors stay open through plan
construction, serialization, stdout write, and flush. Continuous
protection is not claimed after that observation scope ends.

Inventory schema 1 is unchanged: ``cleanup_supported`` stays false,
``cleanup_eligible`` stays false, ``contents_inspected`` stays false,
and ``writer_activity`` stays unknown. ``held_at_scan`` is only
instantaneous cooperative contention. ``not_held_at_scan`` is not
ownership, writer death, abandonment, age, or permission to delete.

Cleanup-plan schema 1 is plan-only (``apply_supported=false``). This
command does not accept ``--expected-plan-revision``, a confirmation
flag, a saved plan file, or an apply result. MCP stays exactly 11
read-only tools; this command is CLI-only.

Usage:
    graphrag-code snapshot-export-staging-cleanup-plan --parent <directory> [--json]
    python -m graphrag_code.snapshot_export_staging_cleanup_plan --parent <directory> [--json]
    uv run python scripts/snapshot_export_staging_cleanup_plan.py --parent <directory> [--json]
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
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

from graphrag_code.snapshot_export_staging import (
    SnapshotExportStagingError,
    SnapshotExportStagingIntegrityError,
    export_staging_observation_scope,
    is_current_export_staging_name,
)

PLAN_SCHEMA_VERSION = 1
_PLAN_REVISION_KEYS = (
    "apply_supported",
    "blocked_entries",
    "cleanup_applied",
    "deletion_candidates",
    "observed_inventory_revision",
    "ownership_inference",
    "schema_version",
)
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "plan_not_authorization",
        "kind": "notice",
        "message": (
            "deletion_candidates means only selected by this read-only "
            "plan. It is not ownership, cleanup eligibility, proof that "
            "apply created the directory, proof that a writer died, or "
            "authorization to delete. A writer may start after the plan "
            "is emitted."
        ),
    },
    {
        "code": "observed_non_contention_not_claim",
        "kind": "notice",
        "message": (
            "not_held_at_scan is only a successful nonblocking probe at "
            "that scan. It is not ownership, writer death, abandonment, "
            "age, or permission to delete. held_at_scan is only "
            "instantaneous cooperative contention and does not change "
            "writer_activity."
        ),
    },
    {
        "code": "apply_not_implemented",
        "kind": "notice",
        "message": (
            "Cleanup-plan schema 1 sets apply_supported=false. This "
            "command does not apply, delete, or accept "
            "--expected-plan-revision, a confirmation flag, a saved "
            "plan file, or an apply result. cleanup_applied stays false."
        ),
    },
    {
        "code": "inventory_semantics_unchanged",
        "kind": "notice",
        "message": (
            "Underlying snapshot-export-staging schema 1 is unchanged. "
            "Inventory cleanup_supported stays false. Recognized "
            "directory cleanup_eligible stays false. contents_inspected "
            "stays false. writer_activity stays unknown. "
            "ownership_inference is false."
        ),
    },
    {
        "code": "cli_only_not_mcp",
        "kind": "notice",
        "message": (
            "snapshot-export-staging-cleanup-plan is CLI-only and "
            "intentionally absent from the fixed 11-tool MCP set."
        ),
    },
)


class SnapshotExportStagingCleanupPlanError(Exception):
    """Malformed arguments, missing parent, bounds, or unsupported platform. Exit 2."""

    exit_code = 2


class SnapshotExportStagingCleanupPlanIntegrityError(
    SnapshotExportStagingCleanupPlanError
):
    """Concurrent listing, identity, metadata, or pathname change. Exit 1."""

    exit_code = 1


def _after_cleanup_plan_ready(parent: Path, result: Mapping[str, Any]) -> None:
    return None


def _byte_sort_names(values: List[str]) -> List[str]:
    return sorted(values, key=os.fsencode)


def _wrap_staging_error(error: Exception) -> SnapshotExportStagingCleanupPlanError:
    if isinstance(error, SnapshotExportStagingIntegrityError):
        return SnapshotExportStagingCleanupPlanIntegrityError(str(error))
    if isinstance(error, SnapshotExportStagingError):
        wrapped = SnapshotExportStagingCleanupPlanError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    return SnapshotExportStagingCleanupPlanError(str(error))


def canonical_plan_revision_payload(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Decision inputs bound by ``plan_revision``. Presentation fields excluded."""
    payload: Dict[str, Any] = {}
    for key in _PLAN_REVISION_KEYS:
        if key not in result:
            raise SnapshotExportStagingCleanupPlanError(
                f"export staging cleanup plan is missing decision input {key!r}"
            )
        payload[key] = result[key]
    return payload


def canonical_plan_revision_text(result: Mapping[str, Any]) -> str:
    """Canonical JSON of the decision inputs. Documented hash input.

    Compact UTF-8 JSON with sorted keys, no trailing newline:
    ``json.dumps(..., sort_keys=True, separators=(",", ":"),
    ensure_ascii=True, allow_nan=False)``.

    Bound keys: ``schema_version``, ``observed_inventory_revision``,
    ``deletion_candidates``, ``blocked_entries``, ``ownership_inference``,
    ``cleanup_applied``, ``apply_supported``. Absolute parent spelling,
    counts, notices, ``ok``, and ``staging_entries`` are excluded.
    ``observed_inventory_revision`` already binds the complete inventory
    observation, including writer-lease state and lock identity.
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
    candidates = ",".join(result.get("deletion_candidates") or [])
    blocked = ",".join(
        f"{item.get('name')}:{item.get('reason')}"
        for item in (result.get("blocked_entries") or [])
    )
    suffix = ""
    if candidates:
        suffix += f" names={candidates}"
    if blocked:
        suffix += f" blocked={blocked}"
    return (
        "snapshot-export-staging-cleanup-plan: "
        f"parent={result.get('parent')} "
        f"staging={result.get('staging_count')} "
        f"unrecognized={result.get('unrecognized_prefixed_count')} "
        f"other={result.get('other_entry_count')} "
        f"deletion_candidates={result.get('deletion_candidate_count')} "
        f"blocked={result.get('blocked_count')} "
        f"observed_inventory_revision={result.get('observed_inventory_revision')} "
        f"plan_revision={result.get('plan_revision')}"
        f"{suffix} "
        "plan-only; apply_supported=false; not authorization to delete"
    )


def _lock_mode_permissive(mode: object) -> bool:
    if not isinstance(mode, int):
        return True
    return bool(stat.S_IMODE(mode) & 0o077)


def _classify_entry(entry: Mapping[str, Any]) -> Tuple[str, Optional[str]]:
    """Return ('candidate', None) or ('blocked', reason)."""
    name = str(entry.get("name") or "")
    if not is_current_export_staging_name(name):
        return "blocked", "unrecognized_staging_name"
    if entry.get("kind") != "directory":
        return "blocked", "non_directory_staging_entry"
    if entry.get("name_matches_current_protocol") is not True:
        return "blocked", "unrecognized_staging_name"
    state = entry.get("writer_lease_state")
    if state == "metadata_absent":
        return "blocked", "writer_lease_metadata_absent"
    if state == "metadata_unsafe":
        return "blocked", "writer_lease_metadata_unsafe"
    if state == "held_at_scan":
        return "blocked", "held_writer_lease"
    if state != "not_held_at_scan":
        return "blocked", "unverifiable_writer_lease_state"
    if (
        entry.get("writer_lease_metadata_present") is not True
        or entry.get("writer_lease_contended") is not False
        or not isinstance(entry.get("writer_lease_size"), int)
        or not isinstance(entry.get("writer_lease_mode"), int)
        or not isinstance(entry.get("writer_lease_dev"), int)
        or not isinstance(entry.get("writer_lease_ino"), int)
        or not isinstance(entry.get("writer_lease_mtime_ns"), int)
        or not isinstance(entry.get("writer_lease_ctime_ns"), int)
    ):
        return "blocked", "unverifiable_writer_lease_state"
    if entry.get("writer_lease_size") != 0:
        return "blocked", "nonempty_writer_lease_metadata"
    if _lock_mode_permissive(entry.get("writer_lease_mode")):
        return "blocked", "permissive_writer_lease_metadata"
    return "candidate", None


def _plan_from_inventory(inventory: Mapping[str, Any]) -> Dict[str, Any]:
    staging = list(inventory.get("staging_entries") or [])
    unrecognized = list(inventory.get("unrecognized_prefixed_entries") or [])
    candidates: List[str] = []
    blocked: List[Dict[str, str]] = []
    for entry in staging + unrecognized:
        name = str(entry.get("name") or "")
        kind, reason = _classify_entry(entry)
        if kind == "candidate":
            candidates.append(name)
        else:
            blocked.append({"name": name, "reason": str(reason)})
    candidates = _byte_sort_names(candidates)
    blocked.sort(key=lambda item: os.fsencode(item["name"]))
    result: Dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "ok": True,
        "parent": inventory["parent"],
        "observed_inventory_revision": inventory["inventory_revision"],
        "staging_entries": staging,
        "staging_count": inventory["staging_count"],
        "unrecognized_prefixed_entries": unrecognized,
        "unrecognized_prefixed_count": inventory["unrecognized_prefixed_count"],
        "other_entry_count": inventory["other_entry_count"],
        "deletion_candidates": candidates,
        "deletion_candidate_count": len(candidates),
        "blocked_entries": blocked,
        "blocked_count": len(blocked),
        "ownership_inference": False,
        "cleanup_applied": False,
        "apply_supported": False,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }
    result["plan_revision"] = plan_revision_of(result)
    return result


@contextmanager
def _snapshot_export_staging_cleanup_plan_scope(
    parent: object,
) -> Iterator[Dict[str, Any]]:
    """Yield one plan while parent and retained probe descriptors stay held."""
    try:
        with export_staging_observation_scope(parent) as inventory:
            result = _plan_from_inventory(inventory)
            _after_cleanup_plan_ready(Path(inventory["parent"]), result)
            yield result
    except SnapshotExportStagingCleanupPlanError:
        raise
    except SnapshotExportStagingError as error:
        raise _wrap_staging_error(error) from error


def snapshot_export_staging_cleanup_plan(parent: Path) -> Dict[str, Any]:
    """Build one export-staging cleanup plan without writing files."""
    with _snapshot_export_staging_cleanup_plan_scope(parent) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report a read-only schema-1 export-staging cleanup plan from "
            "the schema-1 inventory. Does not delete, quarantine, apply, "
            "or infer ownership. apply_supported is false. "
            "cleanup_applied stays false. Does not inspect a managed "
            "graph or export payload contents. Not an MCP tool. "
            "deletion_candidates is not permission to delete."
        )
    )
    parser.add_argument(
        "--parent",
        type=Path,
        required=True,
        help="Parent directory to classify, relative to cwd.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_export_staging_cleanup_plan_scope(args.parent) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            sys.stdout.flush()
            return 0 if result["ok"] else 1
    except SnapshotExportStagingCleanupPlanError as error:
        print(f"snapshot-export-staging-cleanup-plan: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-export-staging-cleanup-plan: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
