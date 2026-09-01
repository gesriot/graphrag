#!/usr/bin/env python
"""Operator-managed snapshot retention pins.

``.snapshot-pins.json`` is durable retention metadata on one managed graph
root. It is not activation, publication, reindexing, backup, replication,
or a distributed lease. Pins protect selected published snapshot ids from
cooperating keep-last cleanup only.

Listing never creates the registry. Pin and unpin may create it only after
explicit confirmation, and unpinning the last entry writes the canonical
empty registry instead of unlinking it.

All three commands require an already-adopted regular ``.publish.lock``.
They never create, truncate, rewrite, chmod, or replace that lock.
Missing lock exits 2 and points at ``adopt-publication-lock``. Advisory
locks protect only cooperating processes. MCP stays exactly 16 read-only
tools; these commands are CLI-only.

Usage:
    graphrag-code snapshot-pins --graph <root> [--json]
    graphrag-code snapshot-pin <id> --graph <root> \\
        --expected-registry-revision <token> --pin-confirmed [--json]
    graphrag-code snapshot-unpin <id> --graph <root> \\
        --expected-registry-revision <token> --unpin-confirmed [--json]
    python -m graphrag_code.snapshot_pins --graph <root> [--json]
    python -m graphrag_code.snapshot_pins pin <id> --graph <root> \\
        --expected-registry-revision <token> --pin-confirmed [--json]
    uv run python scripts/snapshot_pins.py unpin <id> --graph <root> \\
        --expected-registry-revision <token> --unpin-confirmed
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from graphrag_code.byog_graph import (
    PUBLICATION_LOCK_NAME,
    ByogPublicationLockError,
    ByogReaderLockError,
    _atomic_write_text,
    _validate_managed_snapshot_layout,
    graph_exclusive_lease,
    graph_read_lease,
    is_published_snapshot_id,
    is_staging_snapshot_name,
)

OPERATOR_PINS_NAME = ".snapshot-pins.json"
REGISTRY_SCHEMA_VERSION = 1
MAX_REGISTRY_BYTES = 64 * 1024
MAX_PIN_ENTRIES = 1000
ABSENT_REVISION = "absent"
CURRENT_REF = "current"
_REVISION_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_KEYS = frozenset({"schema_version", "pins"})
_MISSING_LOCK_HINT = (
    "A managed graph without a regular .publish.lock is rejected. "
    "This command never creates the lock. Adopt the protocol with "
    "graphrag-code adopt-publication-lock --offline-confirmed."
)
_PIN_CONFIRMATION_MESSAGE = """\
refusing to pin a snapshot without --pin-confirmed.

This is an explicit mutating CLI operation. It writes only
<graph>/.snapshot-pins.json so cooperating keep-last retention will keep
an already-published snapshot. It does not activate, publish, delete,
repair, reindex, back up, or replicate the graph.

--expected-registry-revision is a compare-and-swap guard: the exclusive
existing-lock lease recomputes the registry revision and writes only
when that token still matches. Advisory locks protect only cooperating
processes.
""".strip()
_UNPIN_CONFIRMATION_MESSAGE = """\
refusing to unpin a snapshot without --unpin-confirmed.

This is an explicit mutating CLI operation. It writes only
<graph>/.snapshot-pins.json. Unpin does not delete the snapshot now; it
only makes that id eligible for a later cooperating keep-last operation.
current and remaining claim/operator pins stay protected. It does not
activate, publish, repair, reindex, back up, or replicate the graph.

--expected-registry-revision is a compare-and-swap guard. Advisory locks
protect only cooperating processes.
""".strip()


class SnapshotPinsError(Exception):
    """Expected pin-registry failure. Default exit 2."""

    exit_code = 2


class SnapshotPinsIntegrityError(SnapshotPinsError):
    """CAS mismatch, concurrent mutation, or persisted integrity. Exit 1."""

    exit_code = 1


def _byte_sort(values: Sequence[str]) -> List[str]:
    return sorted(values, key=lambda item: item.encode("utf-8"))


def canonical_registry_text(pins: Sequence[str]) -> str:
    """Return canonical registry JSON text, including the trailing newline."""
    payload = {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "pins": list(pins),
    }
    return (
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )


def revision_of_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def parse_registry_revision(value: str) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotPinsError(
            "expected-registry-revision must be 'absent' or sha256:<hex>"
        )
    if value != value.strip():
        raise SnapshotPinsError(
            "expected-registry-revision must not contain leading or trailing "
            f"whitespace: {value!r}"
        )
    if value == ABSENT_REVISION:
        return ABSENT_REVISION
    if not _REVISION_RE.fullmatch(value):
        raise SnapshotPinsError(
            "expected-registry-revision must be 'absent' or "
            f"sha256:<64 lowercase hex>, got {value!r}"
        )
    return value


def _require_published_pin_id(flag: str, value: object) -> str:
    if not isinstance(value, str) or value == "":
        raise SnapshotPinsError(f"{flag} must be a nonempty published snapshot id")
    if value != value.strip():
        raise SnapshotPinsError(
            f"{flag} must be a canonical published snapshot id, not padded text"
        )
    if value == CURRENT_REF:
        raise SnapshotPinsError(
            f"{flag} must be an explicit published snapshot id, not {value!r}"
        )
    if (
        Path(value).name != value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise SnapshotPinsError(f"unsafe snapshot id: {value!r}")
    if is_staging_snapshot_name(value) or not is_published_snapshot_id(value):
        raise SnapshotPinsError(
            f"staging path is not a published snapshot: {value!r}"
            if is_staging_snapshot_name(value)
            else f"unsafe snapshot id: {value!r}"
        )
    return value


def _strict_json_loads(text: str) -> Any:
    def reject_constant(token: str) -> None:
        raise SnapshotPinsError(f"non-finite JSON constant {token}")

    def unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise SnapshotPinsError(f"duplicate JSON object key {key!r}")
            out[key] = value
        return out

    try:
        return json.loads(
            text, parse_constant=reject_constant, object_pairs_hook=unique_object
        )
    except json.JSONDecodeError as error:
        raise SnapshotPinsError(f"operator pin registry is not valid JSON: {error}") from error


def _parse_registry_document(data: bytes) -> List[str]:
    if len(data) > MAX_REGISTRY_BYTES:
        raise SnapshotPinsError(
            f"operator pin registry exceeds {MAX_REGISTRY_BYTES} bytes"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SnapshotPinsError(
            f"operator pin registry is not UTF-8: {error}"
        ) from error
    parsed = _strict_json_loads(text)
    if not isinstance(parsed, dict):
        raise SnapshotPinsError("operator pin registry must be a JSON object")
    extra = sorted(set(parsed) - _ALLOWED_KEYS)
    missing = sorted(_ALLOWED_KEYS - set(parsed))
    if extra or missing:
        raise SnapshotPinsError(
            "operator pin registry keys must be exactly schema_version and pins"
        )
    version = parsed["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != REGISTRY_SCHEMA_VERSION
    ):
        raise SnapshotPinsError(
            f"operator pin registry schema_version must be {REGISTRY_SCHEMA_VERSION}"
        )
    pins = parsed["pins"]
    if not isinstance(pins, list):
        raise SnapshotPinsError("operator pin registry pins must be a JSON array")
    if len(pins) > MAX_PIN_ENTRIES:
        raise SnapshotPinsError(
            f"operator pin registry exceeds {MAX_PIN_ENTRIES} entries"
        )
    parsed_pins = [
        _require_published_pin_id("operator pin", item) for item in pins
    ]
    canonical = _byte_sort(list(dict.fromkeys(parsed_pins)))
    if parsed_pins != canonical:
        raise SnapshotPinsError(
            "operator pin registry pins must be unique and sorted"
        )
    return parsed_pins


def load_operator_pins_unlocked(graph_root: Path) -> Tuple[str, List[str]]:
    """Read the operator pin registry. Caller must already hold the lease.

    An absent file is an empty pin set with revision ``absent``. Listing and
    publication never create the file. Malformed or unsafe state fails closed
    and is never silently replaced.
    """
    path = Path(graph_root) / OPERATOR_PINS_NAME
    try:
        info = path.lstat()
    except FileNotFoundError:
        return ABSENT_REVISION, []
    except OSError as error:
        raise SnapshotPinsError(
            f"cannot inspect operator pin registry {path}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotPinsError(
            f"unsafe symlinked operator pin registry is unsupported: {path}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotPinsError(
            f"operator pin registry is not a regular file: {path}"
        )
    if info.st_size > MAX_REGISTRY_BYTES:
        raise SnapshotPinsError(
            f"operator pin registry exceeds {MAX_REGISTRY_BYTES} bytes"
        )
    try:
        data = path.read_bytes()
    except OSError as error:
        raise SnapshotPinsError(
            f"cannot read operator pin registry {path}: {error}"
        ) from error
    pins = _parse_registry_document(data)
    return revision_of_bytes(data), pins


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise SnapshotPinsError(f"graph root does not exist: {path}") from error
    except OSError as error:
        raise SnapshotPinsError(f"cannot inspect graph root {path}: {error}") from error
    if stat.S_ISLNK(before.st_mode):
        raise SnapshotPinsError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise SnapshotPinsError(f"graph root is not a real directory: {path}")
    return path.resolve()


def _require_managed_graph(root: Path) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise SnapshotPinsError(str(error)) from error
    if not managed:
        raise SnapshotPinsError(
            "legacy flat-parquet directory has no managed snapshot history to "
            f"pin: {root}"
        )


def _lock_error(error: Exception) -> SnapshotPinsError:
    message = str(error)
    if "publication lock is missing" in message:
        return SnapshotPinsError(f"{message}\n{_MISSING_LOCK_HINT}")
    return SnapshotPinsError(message)


def _lock_identity(root: Path) -> Tuple[int, int, int, int]:
    lock = root / PUBLICATION_LOCK_NAME
    try:
        info = lock.lstat()
    except FileNotFoundError as error:
        raise SnapshotPinsIntegrityError(
            f"publication lock disappeared during pin-registry operation: {lock}"
        ) from error
    except OSError as error:
        raise SnapshotPinsIntegrityError(
            f"cannot inspect publication lock {lock}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotPinsIntegrityError(
            f"unsafe symlinked publication lock is unsupported: {lock}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotPinsIntegrityError(
            f"publication lock is not a regular file: {lock}"
        )
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _read_current_pointer(root: Path) -> str:
    pointer = root / "current"
    try:
        info = pointer.lstat()
    except OSError as error:
        raise SnapshotPinsIntegrityError(
            f"cannot inspect current pointer {pointer}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SnapshotPinsIntegrityError(
            f"unsafe or missing current pointer: {pointer}"
        )
    try:
        snap_id = pointer.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise SnapshotPinsIntegrityError(
            f"cannot read current pointer {pointer}: {error}"
        ) from error
    if not is_published_snapshot_id(snap_id):
        raise SnapshotPinsIntegrityError(
            f"current snapshot id is not a published id: {snap_id!r}"
        )
    return snap_id


def _wrap_compare(error: Exception) -> SnapshotPinsError:
    from graphrag_code.snapshot_compare import SnapshotCompareIntegrityError

    if isinstance(error, SnapshotCompareIntegrityError):
        return SnapshotPinsIntegrityError(str(error))
    return SnapshotPinsError(str(error))


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


def _published_ids(root: Path) -> List[str]:
    from graphrag_code.snapshot_compare import (
        SnapshotCompareError,
        _list_published_and_notices,
    )

    try:
        published, _notices = _list_published_and_notices(root)
    except SnapshotCompareError as error:
        raise _wrap_compare(error) from error
    return list(published)


def _require_existing_published_snapshot(root: Path, snap_id: str) -> None:
    from graphrag_code.snapshot_compare import (
        SnapshotCompareError,
        _resolve_published,
    )

    published = _published_ids(root)
    if snap_id not in published:
        raise SnapshotPinsError(
            f"target snapshot is not a published snapshots/ directory: {snap_id}"
        )
    try:
        snap_dir, resolved, _manifest = _resolve_published(root, snap_id)
    except SnapshotCompareError as error:
        raise _wrap_compare(error) from error
    if resolved != snap_id:
        raise SnapshotPinsError(
            f"resolved snapshot id {resolved!r} does not match {snap_id!r}"
        )
    try:
        info = snap_dir.lstat()
    except FileNotFoundError as error:
        raise SnapshotPinsError(
            f"target snapshot is not a published snapshots/ directory: {snap_id}"
        ) from error
    except OSError as error:
        raise SnapshotPinsError(f"cannot inspect snapshot {snap_dir}: {error}") from error
    if stat.S_ISLNK(info.st_mode):
        raise SnapshotPinsError(f"unsafe symlinked snapshot path: {snap_dir}")
    if not stat.S_ISDIR(info.st_mode):
        raise SnapshotPinsError(f"target snapshot is not a directory: {snap_dir}")


def _payload_fingerprint(root: Path, published: Sequence[str]) -> Dict[str, str]:
    from graphrag_code.snapshot_compare import SnapshotCompareError, _fingerprint

    dirs = [root / "snapshots" / snap_id for snap_id in published]
    try:
        return _fingerprint(root, dirs)
    except SnapshotCompareError as error:
        raise _wrap_compare(error) from error


def _claim_pins(root: Path) -> List[str]:
    from graphrag_code.byog_graph import pinned_snapshot_ids

    return _byte_sort(sorted(pinned_snapshot_ids(root)))


def _view(
    root: Path,
    *,
    current_id: str,
    revision: str,
    operator_pins: Sequence[str],
) -> Dict[str, Any]:
    claim = _claim_pins(root)
    operator = list(operator_pins)
    effective = _byte_sort(list(dict.fromkeys([*operator, *claim])))
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "graph": str(root),
        "current": current_id,
        "registry_revision": revision,
        "operator_pins": operator,
        "claim_pins": claim,
        "effective_pins": effective,
    }


def _registry_path(root: Path) -> Path:
    return root / OPERATOR_PINS_NAME


def _write_registry(root: Path, pins: Sequence[str]) -> Tuple[str, bytes]:
    text = canonical_registry_text(pins)
    data = text.encode("utf-8")
    if len(data) > MAX_REGISTRY_BYTES:
        raise SnapshotPinsError(
            f"operator pin registry exceeds {MAX_REGISTRY_BYTES} bytes"
        )
    path = _registry_path(root)
    try:
        _atomic_write_text(text, path)
    except OSError as error:
        raise SnapshotPinsError(
            f"failed to replace operator pin registry: {error}"
        ) from error
    try:
        written = path.read_bytes()
    except OSError as error:
        raise SnapshotPinsIntegrityError(
            f"cannot re-read operator pin registry after write: {error}"
        ) from error
    if written != data:
        raise SnapshotPinsIntegrityError(
            "operator pin registry bytes did not match the canonical write"
        )
    parsed = _parse_registry_document(written)
    if parsed != list(pins):
        raise SnapshotPinsIntegrityError(
            "operator pin registry did not round-trip to the written pin set"
        )
    return revision_of_bytes(written), written


@contextmanager
def _snapshot_pins_list_scope(graph: Path) -> Iterator[Dict[str, Any]]:
    """Yield the pin view while its shared existing-lock lease remains held."""
    root = _resolve_graph_root(graph)
    try:
        with graph_read_lease(root, allow_unlocked_managed=False):
            # graph_read_lease classifies the layout before it can acquire the
            # graph's lock. Revalidate after acquisition so all pin-registry
            # inputs used for the response are checked inside the lease too.
            _require_managed_graph(root)
            current_id = _resolve_current_once(root)
            revision, operator = load_operator_pins_unlocked(root)
            yield _view(
                root,
                current_id=current_id,
                revision=revision,
                operator_pins=operator,
            )
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_pins_list(graph: Path) -> Dict[str, Any]:
    """Build one pin view without writing files or process streams."""
    with _snapshot_pins_list_scope(graph) as result:
        return result


def _mutate_unlocked(
    root: Path,
    target_id: str,
    expected_revision: str,
    *,
    action: str,
) -> Dict[str, Any]:
    _require_managed_graph(root)
    lock_before = _lock_identity(root)
    current_id = _resolve_current_once(root)
    published = _published_ids(root)
    if current_id not in published:
        raise SnapshotPinsError(
            f"current snapshot is not a published snapshots/ directory: {current_id}"
        )
    _require_existing_published_snapshot(root, target_id)
    before = _payload_fingerprint(root, published)
    observed_current = _read_current_pointer(root)
    if observed_current != current_id:
        raise SnapshotPinsIntegrityError(
            f"current pointer changed before pin-registry write: expected "
            f"{current_id!r}, observed {observed_current!r}"
        )
    revision, operator = load_operator_pins_unlocked(root)
    if revision != expected_revision:
        raise SnapshotPinsIntegrityError(
            f"expected-registry-revision {expected_revision!r} does not match "
            f"observed {revision!r}; refusing to write"
        )
    if _lock_identity(root) != lock_before:
        raise SnapshotPinsIntegrityError(
            "publication lock identity changed during pin-registry operation"
        )
    if list(_published_ids(root)) != list(published):
        raise SnapshotPinsIntegrityError(
            "snapshots listing changed during pin-registry operation"
        )
    prewrite = _payload_fingerprint(root, published)
    if prewrite != before:
        raise SnapshotPinsIntegrityError(
            "protected graph inputs changed during pin-registry operation"
        )

    next_pins = list(operator)
    if action == "pin":
        changed = target_id not in next_pins
        if changed:
            next_pins = _byte_sort([*next_pins, target_id])
    elif action == "unpin":
        changed = target_id in next_pins
        if changed:
            next_pins = [item for item in next_pins if item != target_id]
    else:
        raise SnapshotPinsError(f"unknown pin-registry action: {action}")
    if len(next_pins) > MAX_PIN_ENTRIES:
        raise SnapshotPinsError(
            f"operator pin registry exceeds {MAX_PIN_ENTRIES} entries"
        )

    previous_revision = revision
    if changed:
        # Re-check CAS immediately before the only write.
        recheck, _recheck_pins = load_operator_pins_unlocked(root)
        if recheck != expected_revision:
            raise SnapshotPinsIntegrityError(
                f"expected-registry-revision {expected_revision!r} does not "
                f"match observed {recheck!r}; refusing to write"
            )
        new_revision, _written = _write_registry(root, next_pins)
    else:
        new_revision = revision

    if _read_current_pointer(root) != current_id:
        raise SnapshotPinsIntegrityError(
            "current pointer changed during pin-registry operation"
        )
    if _lock_identity(root) != lock_before:
        raise SnapshotPinsIntegrityError(
            "publication lock identity changed during pin-registry operation"
        )
    if list(_published_ids(root)) != list(published):
        raise SnapshotPinsIntegrityError(
            "snapshots listing changed during pin-registry operation"
        )
    after = _payload_fingerprint(root, published)
    if after != before:
        raise SnapshotPinsIntegrityError(
            "protected graph inputs changed during pin-registry operation"
        )
    observed_revision, observed_pins = load_operator_pins_unlocked(root)
    if observed_revision != new_revision or observed_pins != next_pins:
        raise SnapshotPinsIntegrityError(
            "operator pin registry changed after the canonical write"
        )

    result = _view(
        root,
        current_id=current_id,
        revision=new_revision,
        operator_pins=next_pins,
    )
    result.update(
        {
            "ok": True,
            "changed": changed,
            "previous_registry_revision": previous_revision,
            "registry_revision": new_revision,
        }
    )
    if action == "pin":
        result["pinned_snapshot"] = target_id
    else:
        still_protected = target_id == current_id or target_id in result["claim_pins"]
        result["unpinned_snapshot"] = target_id
        result["immediate_deletion"] = False
        result["eligible_for_future_retention"] = not still_protected
        result["retention_effect"] = (
            "unpin performs no immediate deletion; the snapshot stays on disk "
            "and becomes eligible for a later cooperating keep-last operation "
            "unless it is current or still claim-pinned"
        )
    return result


@contextmanager
def _snapshot_mutation_scope(
    graph: Path,
    snapshot: str,
    expected_registry_revision: str,
    *,
    action: str,
    confirmed: bool,
) -> Iterator[Dict[str, Any]]:
    """Yield one mutation result while its exclusive lease remains held."""
    if action == "pin":
        if not confirmed:
            raise SnapshotPinsError(_PIN_CONFIRMATION_MESSAGE)
    elif action == "unpin":
        if not confirmed:
            raise SnapshotPinsError(_UNPIN_CONFIRMATION_MESSAGE)
    else:
        raise SnapshotPinsError(f"unknown pin-registry action: {action}")
    target_id = _require_published_pin_id("snapshot", snapshot)
    expected = parse_registry_revision(expected_registry_revision)
    root = _resolve_graph_root(graph)
    try:
        with graph_exclusive_lease(root):
            yield _mutate_unlocked(root, target_id, expected, action=action)
    except ByogPublicationLockError as error:
        raise _lock_error(error) from error
    except ByogReaderLockError as error:
        raise _lock_error(error) from error


def snapshot_pin(
    graph: Path,
    snapshot: str,
    expected_registry_revision: str,
    *,
    pin_confirmed: bool,
) -> Dict[str, Any]:
    """Add one published snapshot id to the operator pin registry."""
    with _snapshot_mutation_scope(
        graph,
        snapshot,
        expected_registry_revision,
        action="pin",
        confirmed=pin_confirmed,
    ) as result:
        return result


def snapshot_unpin(
    graph: Path,
    snapshot: str,
    expected_registry_revision: str,
    *,
    unpin_confirmed: bool,
) -> Dict[str, Any]:
    """Remove one published snapshot id from the operator pin registry."""
    with _snapshot_mutation_scope(
        graph,
        snapshot,
        expected_registry_revision,
        action="unpin",
        confirmed=unpin_confirmed,
    ) as result:
        return result


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


def format_result(result: Mapping[str, Any], *, command: str) -> str:
    if command == "snapshot-pins":
        return (
            f"snapshot-pins: graph={result.get('graph')} "
            f"current={result.get('current')} "
            f"registry_revision={result.get('registry_revision')} "
            f"operator_pins={len(result.get('operator_pins') or [])} "
            f"claim_pins={len(result.get('claim_pins') or [])} "
            f"effective_pins={len(result.get('effective_pins') or [])}"
        )
    if command == "snapshot-pin":
        return (
            f"snapshot-pin: graph={result.get('graph')} "
            f"snapshot={result.get('pinned_snapshot')} "
            f"changed={str(result.get('changed')).lower()} "
            f"previous_registry_revision={result.get('previous_registry_revision')} "
            f"registry_revision={result.get('registry_revision')}"
        )
    return (
        f"snapshot-unpin: graph={result.get('graph')} "
        f"snapshot={result.get('unpinned_snapshot')} "
        f"changed={str(result.get('changed')).lower()} "
        f"immediate_deletion=false "
        f"previous_registry_revision={result.get('previous_registry_revision')} "
        f"registry_revision={result.get('registry_revision')}"
    )


def _add_graph_json(parser: argparse.ArgumentParser) -> None:
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


def _normalize_argv(argv: Optional[List[str]]) -> List[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    commands = {"list", "pin", "unpin", "-h", "--help"}
    if not args or args[0] not in commands:
        return ["list", *args]
    return args


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "List or mutate operator-managed snapshot retention pins. Pins "
            "are retention metadata only: they do not activate snapshots, "
            "change current, or delete immediately. Never creates "
            ".publish.lock, and is not an MCP tool."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list", help="list operator, claim, and effective pins")
    _add_graph_json(list_parser)

    pin_parser = sub.add_parser("pin", help="pin a published snapshot id")
    pin_parser.add_argument(
        "snapshot", help="canonical published snapshot id to pin (not 'current')"
    )
    _add_graph_json(pin_parser)
    pin_parser.add_argument(
        "--expected-registry-revision",
        required=True,
        help="absent, or sha256:<hex> of the exact registry file bytes",
    )
    pin_parser.add_argument(
        "--pin-confirmed",
        action="store_true",
        help=(
            "Required to write .snapshot-pins.json. Asserts this is an "
            "explicit operator pin of an already-published snapshot."
        ),
    )

    unpin_parser = sub.add_parser("unpin", help="unpin a published snapshot id")
    unpin_parser.add_argument(
        "snapshot", help="canonical published snapshot id to unpin (not 'current')"
    )
    _add_graph_json(unpin_parser)
    unpin_parser.add_argument(
        "--expected-registry-revision",
        required=True,
        help="absent, or sha256:<hex> of the exact registry file bytes",
    )
    unpin_parser.add_argument(
        "--unpin-confirmed",
        action="store_true",
        help=(
            "Required to write .snapshot-pins.json. Unpin does not delete "
            "the snapshot now."
        ),
    )

    args = parser.parse_args(_normalize_argv(argv))
    command = {
        "list": "snapshot-pins",
        "pin": "snapshot-pin",
        "unpin": "snapshot-unpin",
    }[args.command]
    try:
        if args.command == "list":
            scope = _snapshot_pins_list_scope(args.graph)
        elif args.command == "pin":
            scope = _snapshot_mutation_scope(
                args.graph,
                args.snapshot,
                args.expected_registry_revision,
                action="pin",
                confirmed=bool(args.pin_confirmed),
            )
        else:
            scope = _snapshot_mutation_scope(
                args.graph,
                args.snapshot,
                args.expected_registry_revision,
                action="unpin",
                confirmed=bool(args.unpin_confirmed),
            )
        with scope as result:
            if args.json:
                sys.stdout.write(result_to_json(result))
            else:
                print(format_result(result, command=command))
            # stdout is block-buffered for the normal console-command
            # delegation path. Flush before releasing the graph lease so the
            # complete response is handed to the caller under that lease.
            sys.stdout.flush()
    except SnapshotPinsError as error:
        print(f"{command}: {error}", file=sys.stderr)
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"{command}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
