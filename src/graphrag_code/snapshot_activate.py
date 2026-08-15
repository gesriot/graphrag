#!/usr/bin/env python
"""Explicit CLI-only activation of an already-published retained snapshot.

Changes only the managed graph's ``current`` pointer so an operator can
select a retained published snapshot. This is not deletion, retention,
repair, reindexing, or publication. It is intentionally absent from MCP.

``--activate-confirmed`` and ``--expected-current`` are mandatory. The
expected-current value is a compare-and-swap guard: while one exclusive
existing-lock lease is held, ``current`` is resolved exactly once and the
pointer is written only when that id still matches. Activating the
already-current snapshot is a successful idempotent no-op.

The command requires an already-adopted regular ``.publish.lock``. It
never creates, truncates, rewrites, chmods, or replaces the lock.
Missing lock exits 2 and points at ``adopt-publication-lock``. Advisory
locks protect only cooperating processes.

Usage:
    graphrag-code snapshot-activate --graph <root> --snapshot <id> \\
        --expected-current <id> --activate-confirmed
    python -m graphrag_code.snapshot_activate --graph <root> --snapshot <id> \\
        --expected-current <id> --activate-confirmed --json
    uv run python scripts/snapshot_activate.py --graph <root> --snapshot <id> \\
        --expected-current <id> --activate-confirmed
"""
from __future__ import annotations

import argparse
import json
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from graphrag_code.byog_graph import (
    PUBLICATION_LOCK_NAME,
    ByogPublicationLockError,
    ByogReaderLockError,
    _atomic_write_text,
    _validate_managed_snapshot_layout,
    graph_exclusive_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
)
from graphrag_code.byog_snapshot_graph_audit import SnapshotGraphAuditError
from graphrag_code.persisted_graph_doctor import PersistedGraphDoctorError
from graphrag_code.snapshot_compare import (
    CURRENT_REF,
    SnapshotCompareError,
    SnapshotCompareIntegrityError,
    _fingerprint,
    _list_published_and_notices,
    _load_tables,
    _resolve_current_once,
    _resolve_published,
    _validate_envelope,
    _verify_discovery_after_baseline,
)

_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_CONFIRMATION_MESSAGE = """\
refusing to activate a snapshot without --activate-confirmed.

This is an explicit mutating CLI operation. It changes only <graph>/current
to an already-published retained snapshot. It does not delete, retain,
publish, repair, or reindex.

--expected-current is a compare-and-swap guard: current is resolved once
under the exclusive existing-lock lease, and the pointer is written only
when that id still matches. Advisory locks protect only cooperating
processes.
""".strip()


class SnapshotActivateError(Exception):
    """Expected activation failure. Default exit 2."""

    exit_code = 2


class SnapshotActivateIntegrityError(SnapshotActivateError):
    """Persisted integrity, CAS mismatch, or concurrent mutation. Exit 1."""

    exit_code = 1


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotActivateError(f"graph root does not exist: {path}") from error
    except OSError as error:
        raise SnapshotActivateError(f"cannot inspect graph root {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotActivateError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotActivateError(f"graph root is not a real directory: {path}")
    return path.resolve()


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotActivateError(str(error)) from error
    if not managed:
        raise SnapshotActivateError(
            "legacy flat-parquet directory has no managed current pointer to "
            f"activate: {root}"
        )


def _require_explicit_published_id(flag: str, value: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise SnapshotActivateError(
            f"{flag} must be a nonempty published snapshot id"
        )
    if value != value.strip():
        raise SnapshotActivateError(
            f"{flag} must be a canonical published snapshot id, not padded text"
        )
    if value == CURRENT_REF:
        raise SnapshotActivateError(
            f"{flag} must be an explicit published snapshot id, not {value!r}"
        )
    if (
        Path(value).name != value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise SnapshotActivateError(f"unsafe snapshot id: {value!r}")
    if is_staging_snapshot_name(value) or not is_published_snapshot_id(value):
        raise SnapshotActivateError(
            f"staging path is not a published snapshot: {value!r}"
            if is_staging_snapshot_name(value)
            else f"unsafe snapshot id: {value!r}"
        )
    return value


def _lock_error(error: Exception) -> SnapshotActivateError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotActivateError(f"{message}\n{_MISSING_LOCK_HINT}")
    return SnapshotActivateError(message)


def _wrap_compare(error: SnapshotCompareError) -> SnapshotActivateError:
    if isinstance(error, SnapshotCompareIntegrityError):
        return SnapshotActivateIntegrityError(str(error))
    return SnapshotActivateError(str(error))


def _lock_identity(root: Path) -> Tuple[int, int, int, int]:
    lock = root / PUBLICATION_LOCK_NAME
    try:
        info = lock.lstat()
    except FileNotFoundError as error:
        raise SnapshotActivateIntegrityError(
            f"publication lock disappeared during activation: {lock}"
        ) from error
    except OSError as error:
        raise SnapshotActivateIntegrityError(
            f"cannot inspect publication lock {lock}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotActivateIntegrityError(
            f"unsafe symlinked publication lock is unsupported: {lock}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotActivateIntegrityError(
            f"publication lock is not a regular file: {lock}"
        )
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_current_pointer(root: Path) -> str:
    pointer = root / "current"
    try:
        info = pointer.lstat()
    except OSError as error:
        raise SnapshotActivateIntegrityError(
            f"cannot inspect current pointer {pointer}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SnapshotActivateIntegrityError(
            f"unsafe or missing current pointer: {pointer}"
        )
    try:
        snap_id = pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise SnapshotActivateIntegrityError(
            f"cannot read current pointer {pointer}: {error}"
        ) from error
    if not is_published_snapshot_id(snap_id):
        raise SnapshotActivateIntegrityError(
            f"current snapshot id is not a published id: {snap_id!r}"
        )
    return snap_id


def _payload_keys(fingerprint: Mapping[str, str]) -> List[str]:
    return sorted(
        key
        for key in fingerprint
        if key.startswith("snapshot/") or key.endswith("/snapshot/listing")
        or "/snapshot/" in key
    )


def _verify_activation_fingerprints(
    before: Mapping[str, str],
    after: Mapping[str, str],
    *,
    expect_current_change: bool,
) -> Tuple[bool, bool]:
    changed = sorted(name for name in before if before[name] != after.get(name))
    changed.extend(sorted(name for name in after if name not in before))
    changed = sorted(set(changed))
    unexpected = [name for name in changed if name != "graph/current"]
    if unexpected:
        raise SnapshotActivateIntegrityError(
            f"protected graph inputs changed during activation: {unexpected}"
        )
    current_changed = "graph/current" in changed
    if expect_current_change and not current_changed:
        raise SnapshotActivateIntegrityError(
            "current pointer did not change to the activated snapshot"
        )
    if not expect_current_change and current_changed:
        raise SnapshotActivateIntegrityError(
            "current pointer changed during an idempotent activation"
        )
    listing_unchanged = before.get("graph/snapshots_listing") == after.get(
        "graph/snapshots_listing"
    )
    if not listing_unchanged:
        raise SnapshotActivateIntegrityError(
            "snapshots listing changed during activation"
        )
    lock_unchanged = before.get("graph/publish_lock") == after.get("graph/publish_lock")
    if not lock_unchanged:
        raise SnapshotActivateIntegrityError(
            "publication lock changed during activation"
        )
    payload_unchanged = all(
        before.get(key) == after.get(key) for key in _payload_keys(before)
    ) and all(key in before for key in _payload_keys(after))
    if not payload_unchanged:
        raise SnapshotActivateIntegrityError(
            "published snapshot payloads changed during activation"
        )
    return payload_unchanged, listing_unchanged


def _snapshot_activate_unlocked(
    root: Path,
    target_id: str,
    expected_current: str,
) -> Dict[str, Any]:
    """Activate ``target_id``. Caller must already hold ``graph_exclusive_lease``."""
    _require_managed_graph(root)
    lock_before = _lock_identity(root)
    try:
        current_dir, current_id, _manifest = _resolve_current_once(root)
        published, _preliminary_notices = _list_published_and_notices(root)
    except SnapshotCompareError as error:
        raise _wrap_compare(error) from error
    if current_id != expected_current:
        raise SnapshotActivateIntegrityError(
            f"expected-current {expected_current!r} does not match resolved "
            f"current {current_id!r}; refusing to write"
        )
    if current_id not in published:
        raise SnapshotActivateError(
            f"current snapshot is not a published snapshots/ directory: {current_id}"
        )
    if target_id not in published:
        raise SnapshotActivateIntegrityError(
            f"target snapshot is not a published snapshots/ directory: {target_id}"
        )
    fingerprint_dirs = [root / "snapshots" / snap_id for snap_id in published]
    try:
        before = _fingerprint(root, fingerprint_dirs)
        notices = _verify_discovery_after_baseline(
            root,
            current_dir=current_dir,
            current_id=current_id,
            published=published,
        )
        snap_dir, resolved, manifest = _resolve_published(root, target_id)
        tables = _load_tables(snap_dir)
        _validate_envelope(resolved, manifest, tables, graph=root)
    except SnapshotCompareError as error:
        raise _wrap_compare(error) from error

    try:
        checked_published, _ = _list_published_and_notices(root)
    except SnapshotCompareError as error:
        raise _wrap_compare(error) from error
    if list(checked_published) != list(published):
        raise SnapshotActivateIntegrityError(
            "snapshots listing changed during activation"
        )
    # Validate the complete protected state once more immediately before the
    # only write. This prevents a lock-ignoring mutation that happens after
    # target validation from needlessly leaving ``current`` changed before the
    # final fingerprint reports the conflict.
    try:
        prewrite = _fingerprint(root, fingerprint_dirs)
    except SnapshotCompareError as error:
        raise _wrap_compare(error) from error
    _verify_activation_fingerprints(
        before,
        prewrite,
        expect_current_change=False,
    )
    observed_before_write = _read_current_pointer(root)
    if observed_before_write != current_id:
        raise SnapshotActivateIntegrityError(
            f"current pointer changed before activation: expected {current_id!r}, "
            f"observed {observed_before_write!r}"
        )
    if _lock_identity(root) != lock_before:
        raise SnapshotActivateIntegrityError(
            "publication lock identity changed during activation"
        )

    changed = current_id != target_id
    if changed:
        try:
            _atomic_write_text(target_id, root / "current")
        except OSError as error:
            raise SnapshotActivateError(
                f"failed to replace current pointer: {error}"
            ) from error

    observed = _read_current_pointer(root)
    if observed != target_id:
        raise SnapshotActivateIntegrityError(
            f"current pointer is {observed!r} after activation, expected {target_id!r}"
        )
    try:
        checked_published, _ = _list_published_and_notices(root)
    except SnapshotCompareError as error:
        raise _wrap_compare(error) from error
    if list(checked_published) != list(published):
        raise SnapshotActivateIntegrityError(
            "snapshots listing changed during activation"
        )
    if _lock_identity(root) != lock_before:
        raise SnapshotActivateIntegrityError(
            "publication lock identity changed during activation"
        )
    try:
        after = _fingerprint(root, fingerprint_dirs)
    except SnapshotCompareError as error:
        raise _wrap_compare(error) from error
    payload_unchanged, listing_unchanged = _verify_activation_fingerprints(
        before,
        after,
        expect_current_change=changed,
    )
    return {
        "ok": True,
        "graph": str(root),
        "previous_current": current_id,
        "expected_current": expected_current,
        "activated_snapshot": target_id,
        "current": target_id,
        "changed": changed,
        "target_integrity": {
            "ok": True,
            "status": "valid",
            "n_anomalies": 0,
        },
        "payload_unchanged": payload_unchanged,
        "snapshots_listing_unchanged": listing_unchanged,
        "publication_notices": notices,
        "read_only_verification": {
            "verified": True,
            "changed_inputs": [],
        },
    }


def snapshot_activate(
    graph: Path,
    snapshot: str,
    expected_current: str,
    *,
    activate_confirmed: bool,
) -> Dict[str, Any]:
    """Activate a published snapshot by replacing only ``current``.

    Returns the stable result object. Raises :class:`SnapshotActivateError`
    (exit 2) or :class:`SnapshotActivateIntegrityError` (exit 1). Never
    creates ``.publish.lock``.
    """
    if not activate_confirmed:
        raise SnapshotActivateError(_CONFIRMATION_MESSAGE)
    target_id = _require_explicit_published_id("--snapshot", snapshot)
    expected_id = _require_explicit_published_id(
        "--expected-current", expected_current
    )
    root = _resolve_graph_root(graph)
    _require_managed_graph(root)
    try:
        with graph_exclusive_lease(root):
            return _snapshot_activate_unlocked(root, target_id, expected_id)
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


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
        f"snapshot-activate: graph={result.get('graph')} "
        f"previous_current={result.get('previous_current')} "
        f"current={result.get('current')} "
        f"changed={str(result.get('changed')).lower()}"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Activate an already-published retained snapshot by changing only "
            "the managed graph's current pointer. Requires --activate-confirmed "
            "and --expected-current. Never creates .publish.lock, never deletes "
            "snapshots, and is not an MCP tool."
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
        help="canonical published snapshot id to activate (not 'current')",
    )
    parser.add_argument(
        "--expected-current",
        required=True,
        help="canonical published id that current must still name",
    )
    parser.add_argument(
        "--activate-confirmed",
        action="store_true",
        help=(
            "Required to change current. Asserts this is an explicit operator "
            "activation of a retained published snapshot. The command still "
            "refuses to write if current no longer matches --expected-current."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        result = snapshot_activate(
            args.graph,
            args.snapshot,
            args.expected_current,
            activate_confirmed=bool(args.activate_confirmed),
        )
    except SnapshotActivateError as error:
        print(f"snapshot-activate: {error}", file=sys.stderr)
        return error.exit_code
    except (SnapshotGraphAuditError, PersistedGraphDoctorError) as error:
        print(f"snapshot-activate: {error}", file=sys.stderr)
        return 2
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"snapshot-activate: {error}", file=sys.stderr)
        return 2

    if args.json:
        sys.stdout.write(result_to_json(result))
    else:
        print(format_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
