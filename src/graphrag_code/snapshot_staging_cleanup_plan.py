#!/usr/bin/env python
"""Read-only staging cleanup plan.

``snapshot-staging-cleanup-plan`` classifies every direct
``snapshots/.staging-*`` entry from the existing schema-2 inventory.
It does not delete, rename, quarantine, repair, claim, or otherwise
mutate staging entries. ``deletion_candidates`` means only "candidate
in this read-only plan". It is not proof that a writer died, not
ownership, not permission to delete, not a durable lease, and not
``cleanup_eligible`` in the inventory schema. A future writer may
acquire the private writer lease after this plan is emitted.

This command reuses the snapshot-staging two-scan scanner under one
shared existing-lock graph lease. It never creates, truncates,
rewrites, chmods, or replaces ``.publish.lock``. It never creates or
changes ``.snapshot-pins.json``, ``current``, published snapshots,
staging directories, payload files, or writer-lock bytes/metadata.
It does not take a nested graph-root lease. MCP stays exactly 17
read-only tools; this command is CLI-only.

Cleanup-plan schema 1 was read-only/pre-apply
(``apply_supported=false``). Schema 2 is the CAS input accepted by
``snapshot-staging-cleanup``. This command still does not apply or
accept ``--expected-plan-revision``. ``cleanup_applied`` stays false.
``apply_supported`` is true because a separate exclusive apply command
exists. Observed non-contention here is not that apply's exclusive
writer-lock claim. Schema-1 plan revisions must not be accepted by
apply.

Usage:
    graphrag-code snapshot-staging-cleanup-plan --graph <root> [--json]
    python -m graphrag_code.snapshot_staging_cleanup_plan --graph <root> [--json]
    uv run python scripts/snapshot_staging_cleanup_plan.py --graph <root> [--json]
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
from graphrag_code.snapshot_staging import (
    SnapshotStagingError,
    SnapshotStagingIntegrityError,
    build_stable_staging_inventory_unlocked,
    staging_state_revision_of,
)

PLAN_SCHEMA_VERSION = 2
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_PLAN_REVISION_KEYS = (
    "apply_supported",
    "blocked_entries",
    "cleanup_applied",
    "current",
    "deletion_candidates",
    "observed_staging_revision",
    "ownership_inference",
    "published_snapshots",
    "schema_version",
    "staging_state_revision",
)
_COMMAND_NOTICES: Tuple[Dict[str, str], ...] = (
    {
        "code": "plan_not_authorization",
        "kind": "notice",
        "message": (
            "deletion_candidates means only candidate in this read-only "
            "plan. It is not proof that a writer died, not ownership, "
            "not permission to delete, not a durable lease, and not "
            "inventory cleanup_eligible. A future writer may acquire "
            "the private writer lease after this plan is emitted."
        ),
    },
    {
        "code": "observed_non_contention_not_claim",
        "kind": "notice",
        "message": (
            "A successful nonblocking probe means only that the private "
            "staging-writer lease was not held at that scan. That is "
            "not a graph-root exclusive claim and not the apply "
            "command's exclusive writer-lock claim. Apply recomputes "
            "this plan and claims existing writer locks itself."
        ),
    },
    {
        "code": "apply_is_separate_cas_command",
        "kind": "notice",
        "message": (
            "Cleanup-plan schema 2 sets apply_supported=true. Schema 1 "
            "was read-only/pre-apply and is not accepted by apply. "
            "snapshot-staging-cleanup is the separate CAS apply: it "
            "acquires the graph-root exclusive existing-lock lease, "
            "recomputes and compares this plan_revision, nonblockingly "
            "claims every selected existing writer lock, revalidates "
            "staged directory and writer-lock identities, and holds "
            "those claims through deletion. This command does not "
            "accept or apply a revision. cleanup_applied stays false."
        ),
    },
    {
        "code": "inventory_cleanup_eligible_false",
        "kind": "notice",
        "message": (
            "Underlying snapshot-staging entries keep "
            "cleanup_eligible=false. This plan does not change "
            "inventory schema 2."
        ),
    },
)


class SnapshotStagingCleanupPlanError(Exception):
    """Expected cleanup-plan failure. Default exit 2."""

    exit_code = 2


class SnapshotStagingCleanupPlanIntegrityError(SnapshotStagingCleanupPlanError):
    """Persisted integrity, symlink, or concurrent mutation. Exit 1."""

    exit_code = 1


def _byte_sort_names(values: List[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def _wrap_staging_error(error: Exception) -> SnapshotStagingCleanupPlanError:
    if isinstance(error, SnapshotStagingIntegrityError):
        return SnapshotStagingCleanupPlanIntegrityError(str(error))
    if isinstance(error, SnapshotStagingError):
        wrapped = SnapshotStagingCleanupPlanError(str(error))
        wrapped.exit_code = getattr(error, "exit_code", 2)
        return wrapped
    return SnapshotStagingCleanupPlanError(str(error))


def canonical_plan_revision_payload(result: Mapping[str, Any]) -> Dict[str, Any]:
    """Decision inputs bound by ``plan_revision``. Presentation fields excluded."""
    payload: Dict[str, Any] = {}
    for key in _PLAN_REVISION_KEYS:
        if key not in result:
            raise SnapshotStagingCleanupPlanError(
                f"staging cleanup plan is missing decision input {key!r}"
            )
        payload[key] = result[key]
    return payload


def canonical_plan_revision_text(result: Mapping[str, Any]) -> str:
    """Canonical JSON of the decision inputs. Documented hash input.

    Compact UTF-8 JSON with sorted keys, no trailing newline:
    ``json.dumps(..., sort_keys=True, separators=(",", ":"),
    ensure_ascii=True, allow_nan=False)``.

    Bound keys: ``schema_version``, ``current``, ``published_snapshots``,
    ``observed_staging_revision``, ``staging_state_revision``,
    ``deletion_candidates``, ``blocked_entries``, ``ownership_inference``,
    ``cleanup_applied``, ``apply_supported``. Absolute graph path,
    counts, notices, ``ok``, and ``staging_entries`` are excluded.
    ``staging_state_revision`` already binds internal identities that
    public inventory fields omit.
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
        "snapshot-staging-cleanup-plan: "
        f"graph={result.get('graph')} "
        f"current={result.get('current')} "
        f"published={len(result.get('published_snapshots') or [])} "
        f"staging={result.get('staging_count')} "
        f"deletion_candidates={result.get('deletion_candidate_count')} "
        f"blocked={result.get('blocked_count')} "
        f"observed_staging_revision={result.get('observed_staging_revision')} "
        f"staging_state_revision={result.get('staging_state_revision')} "
        f"plan_revision={result.get('plan_revision')}"
        f"{suffix}"
    )


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotStagingCleanupPlanError(
            f"graph root does not exist: {path}"
        ) from error
    except OSError as error:
        raise SnapshotStagingCleanupPlanError(
            f"cannot inspect graph root {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotStagingCleanupPlanError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotStagingCleanupPlanError(
            f"graph root is not a real directory: {path}"
        )
    return path.resolve()


def _lock_error(error: Exception) -> SnapshotStagingCleanupPlanError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotStagingCleanupPlanError(f"{message}\n{_MISSING_LOCK_HINT}")
    if (
        "publication lock changed" in message
        or "publication lock disappeared" in message
    ):
        return SnapshotStagingCleanupPlanIntegrityError(message)
    return SnapshotStagingCleanupPlanError(message)


def _classify_entry(entry: Mapping[str, Any]) -> Tuple[str, Optional[str]]:
    """Return ('candidate', None) or ('blocked', reason)."""
    if entry.get("entry_kind") != "directory":
        return "blocked", "non_directory_staging_entry"
    if entry.get("name_valid") is not True:
        return "blocked", "noncanonical_staging_name"
    if entry.get("writer_lease_state") == "held_by_cooperating_writer":
        return "blocked", "held_writer_lease"
    if (
        entry.get("writer_lease_protocol") == "cooperative_v1"
        and entry.get("writer_lease_state") == "not_held_at_scan"
        and entry.get("writer_lock_present") is True
        and entry.get("writer_lock_regular") is True
    ):
        return "candidate", None
    return "blocked", "legacy_or_missing_writer_lock"


def _plan_from_stable_inventory(
    inventory: Mapping[str, Any], consistency: Mapping[str, Any]
) -> Dict[str, Any]:
    entries = list(inventory.get("staging_entries") or [])
    candidates: List[str] = []
    blocked: List[Dict[str, str]] = []
    for entry in entries:
        name = str(entry.get("name") or "")
        kind, reason = _classify_entry(entry)
        if kind == "candidate":
            candidates.append(name)
        else:
            blocked.append({"name": name, "reason": str(reason)})
    candidates = _byte_sort_names(candidates)
    blocked.sort(key=lambda item: item["name"].encode("utf-8"))
    result: Dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "ok": True,
        "graph": inventory["graph"],
        "current": inventory["current"],
        "published_snapshots": list(inventory["published_snapshots"]),
        "observed_staging_revision": inventory["staging_revision"],
        "staging_state_revision": staging_state_revision_of(consistency),
        "staging_count": inventory["staging_count"],
        "staging_entries": entries,
        "deletion_candidates": candidates,
        "deletion_candidate_count": len(candidates),
        "blocked_entries": blocked,
        "blocked_count": len(blocked),
        "ownership_inference": False,
        "cleanup_applied": False,
        "apply_supported": True,
        "notices": [dict(notice) for notice in _COMMAND_NOTICES],
    }
    result["plan_revision"] = plan_revision_of(result)
    return result


def build_stable_cleanup_plan_unlocked(
    root: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Two-scan cleanup plan. Caller must already hold the graph lease.

    Returns ``(consistency, plan)``. ``consistency`` is the internal
    stable scan token used by ``staging_state_revision`` and by apply
    revalidation. Do not expose it.
    """
    try:
        consistency, inventory = build_stable_staging_inventory_unlocked(root)
    except SnapshotStagingError as error:
        raise _wrap_staging_error(error) from error
    return consistency, _plan_from_stable_inventory(inventory, consistency)


def _build_plan_unlocked(root: Path) -> Dict[str, Any]:
    _consistency, plan = build_stable_cleanup_plan_unlocked(root)
    return plan


@contextmanager
def _snapshot_staging_cleanup_plan_scope(graph: Path) -> Iterator[Dict[str, Any]]:
    """Yield one plan while its shared existing-lock lease remains held."""
    root = _resolve_graph_root(graph)
    try:
        with graph_read_lease(root, allow_unlocked_managed=False):
            yield _build_plan_unlocked(root)
    except SnapshotStagingCleanupPlanError:
        raise
    except SnapshotStagingError as error:
        raise _wrap_staging_error(error) from error
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_staging_cleanup_plan(graph: Path) -> Dict[str, Any]:
    """Build one staging cleanup plan without writing files or process streams."""
    with _snapshot_staging_cleanup_plan_scope(graph) as result:
        return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Report a read-only schema-2 staging cleanup plan from the "
            "schema-2 inventory. Does not delete, quarantine, apply, "
            "or infer ownership. apply_supported is true because "
            "snapshot-staging-cleanup is the separate CAS apply. "
            "cleanup_applied stays false. Never creates .publish.lock "
            "or .snapshot-pins.json, and is not an MCP tool. "
            "deletion_candidates is not permission to delete."
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
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        with _snapshot_staging_cleanup_plan_scope(args.graph) as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing the graph lease so the
            # complete response is handed to the caller under that lease.
            sys.stdout.flush()
    except SnapshotStagingCleanupPlanError as error:
        print(f"snapshot-staging-cleanup-plan: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-staging-cleanup-plan: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
