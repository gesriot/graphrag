#!/usr/bin/env python
"""Explicit offline adoption of the graph-root ``.publish.lock`` protocol.

Adds a regular ``.publish.lock`` to a doctor-valid managed
``current + snapshots/`` graph that was published before the lock existed.
This is an operator-confirmed migration, never an automatic MCP or doctor
side effect. It does not reindex, extract, compile, publish, retain, or
rewrite graph payload. The only pathname it may create is
``<graph>/.publish.lock``.

Creating that file requires ``--offline-confirmed``. Passing the flag
asserts that:

- no legacy reader that ignores ``.publish.lock`` is active;
- no legacy publisher or retention process that ignores ``.publish.lock``
  is active;
- future publishers will use the current lock-aware protocol.

This program cannot discover or prove those conditions. Readers and
publishers that never open ``.publish.lock`` are invisible here. Creating
the lock while such a process is live would split the locking domain.

Without ``--offline-confirmed``, a managed graph that is missing the lock
exits 2 and is not modified.

Usage:
    graphrag-code adopt-publication-lock --graph <root> --indexer auto --offline-confirmed
    python -m graphrag_code.adopt_publication_lock --graph <root> --indexer python --offline-confirmed --json
    uv run python scripts/adopt_publication_lock.py --graph <root> --indexer c --offline-confirmed
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from graphrag_code.byog_graph import (  # type: ignore
    PUBLICATION_LOCK_NAME,
    ByogPublicationLockError,
    ByogReaderLockError,
    _acquire_exclusive_lock,
    _available_lock_backend,
    _release_exclusive_lock,
    _validate_managed_snapshot_layout,
    is_published_snapshot_id,
)
from graphrag_code.byog_snapshot_graph_audit import SnapshotGraphAuditError  # type: ignore
from graphrag_code.persisted_graph_doctor import (  # type: ignore
    PersistedGraphDoctorError,
    _audit_graph_root_unlocked,
    audit_graph_root,
)
from graphrag_code.persisted_graph_integrity import AmbiguousIndexerError  # type: ignore

OFFLINE_ASSUMPTION = "operator-confirmed"
RESULT_KEYS = (
    "ok",
    "status",
    "graph",
    "lock",
    "lock_created",
    "indexer",
    "snapshot",
    "payload_unchanged",
    "offline_assumption",
)
_QUIESCENCE_HELP = (
    "Required to create .publish.lock. Asserts that no legacy reader that "
    "ignores .publish.lock is active, no legacy publisher or retention "
    "process that ignores .publish.lock is active, and future publishers "
    "will use the current lock-aware protocol. This program cannot prove "
    "those conditions."
)
_QUIESCENCE_MESSAGE = """\
refusing to create .publish.lock without --offline-confirmed.

This is an explicit offline migration. Passing --offline-confirmed asserts that:
  - no legacy reader that ignores .publish.lock is active
  - no legacy publisher/retention process that ignores .publish.lock is active
  - future publishers will use the current lock-aware protocol

This program cannot discover or prove that those conditions hold.
It cannot see readers or publishers that never open .publish.lock.
Creating the lock while such a process is live would split the locking domain.

A managed graph without a regular .publish.lock is rejected by MCP and by
strict graph_read_lease(). Adoption never reindexes or rewrites graph payload.
"""


class AdoptPublicationLockError(Exception):
    """Expected adoption failure. ``exit_code`` is 2 unless overridden."""

    exit_code = 2

    def __init__(
        self,
        message: str,
        *,
        exit_code: Optional[int] = None,
        lock_created: bool = False,
        lock_path: Optional[Path] = None,
    ) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code
        self.lock_created = lock_created
        self.lock_path = lock_path


class AdoptIntegrityError(AdoptPublicationLockError):
    """Persisted-integrity failure. Exit 1."""

    exit_code = 1


def _resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise AdoptPublicationLockError(
            f"graph root does not exist: {path}"
        ) from error
    except OSError as error:
        raise AdoptPublicationLockError(
            f"cannot inspect graph root {path}: {error}"
        ) from error
    if stat.S_ISLNK(before.st_mode):
        raise AdoptPublicationLockError(
            f"graph root must be a real directory, not a symlink: {path}"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise AdoptPublicationLockError(
            f"graph root is not a real directory: {path}"
        )
    return path.resolve()


def _require_valid_current_snapshot(root: Path) -> str:
    current = root / "current"
    try:
        snap_id = current.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise AdoptPublicationLockError(
            f"cannot read current pointer {current}: {error}"
        ) from error
    if not is_published_snapshot_id(snap_id):
        raise AdoptPublicationLockError(
            f"current snapshot id is not a published id: {snap_id!r}"
        )
    snap_dir = root / "snapshots" / snap_id
    try:
        info = snap_dir.lstat()
    except FileNotFoundError as error:
        raise AdoptPublicationLockError(
            f"current snapshot is missing: {snap_dir}"
        ) from error
    except OSError as error:
        raise AdoptPublicationLockError(
            f"cannot inspect current snapshot {snap_dir}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise AdoptPublicationLockError(
            f"unsafe symlinked current snapshot is unsupported: {snap_dir}"
        )
    if not stat.S_ISDIR(info.st_mode):
        raise AdoptPublicationLockError(
            f"current snapshot is not a real directory: {snap_dir}"
        )
    return snap_id


def _require_adoptable_managed_graph(root: Path) -> str:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise AdoptPublicationLockError(str(error)) from error
    if not managed:
        raise AdoptPublicationLockError(
            "legacy flat-parquet directory is not adoptable because it has "
            f"no managed publication/retention protocol: {root}"
        )
    return _require_valid_current_snapshot(root)


def _inspect_lock_path(lock_path: Path) -> Optional[os.stat_result]:
    try:
        info = lock_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise AdoptPublicationLockError(
            f"cannot inspect publication lock {lock_path}: {error}"
        ) from error
    if stat.S_ISLNK(info.st_mode):
        raise AdoptPublicationLockError(
            f"unsafe symlinked publication lock is unsupported: {lock_path}"
        )
    if not stat.S_ISREG(info.st_mode):
        raise AdoptPublicationLockError(
            f"publication lock is not a regular file: {lock_path}"
        )
    return info


def _validate_opened_lock_fd(
    fd: int,
    lock_path: Path,
    before: Optional[os.stat_result],
) -> None:
    opened = os.fstat(fd)
    if not stat.S_ISREG(opened.st_mode):
        raise AdoptPublicationLockError(
            f"publication lock is not a regular file: {lock_path}"
        )
    if before is not None and (
        before.st_dev != opened.st_dev or before.st_ino != opened.st_ino
    ):
        raise AdoptPublicationLockError(
            f"publication lock changed while opening it: {lock_path}"
        )
    try:
        current = lock_path.lstat()
    except OSError as error:
        raise AdoptPublicationLockError(
            f"cannot re-inspect publication lock {lock_path}: {error}"
        ) from error
    if stat.S_ISLNK(current.st_mode):
        raise AdoptPublicationLockError(
            f"unsafe symlinked publication lock is unsupported: {lock_path}"
        )
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        raise AdoptPublicationLockError(
            f"publication lock changed while opening it: {lock_path}"
        )


def _open_existing_regular_lock_fd(lock_path: Path) -> int:
    before = _inspect_lock_path(lock_path)
    if before is None:
        raise AdoptPublicationLockError(
            f"publication lock disappeared while opening it: {lock_path}"
        )
    flags = os.O_RDWR
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(lock_path), flags)
    except OSError as error:
        if getattr(error, "errno", None) == getattr(errno, "ELOOP", object()):
            raise AdoptPublicationLockError(
                f"unsafe symlinked publication lock is unsupported: {lock_path}"
            ) from error
        raise AdoptPublicationLockError(
            f"cannot open publication lock {lock_path}: {error}"
        ) from error
    try:
        _validate_opened_lock_fd(fd, lock_path, before)
    except Exception:
        os.close(fd)
        raise
    return fd


def _open_adoption_lock_fd(lock_path: Path) -> Tuple[int, bool]:
    """Open an existing regular lock or exclusively create a missing one.

    Never follows a symlink, never replaces/truncates/chmods/rewrites an
    existing pathname, and never unlinks a file after the pathname is
    exposed. Returns ``(fd, created)``.
    """
    existing = _inspect_lock_path(lock_path)
    if existing is not None:
        return _open_existing_regular_lock_fd(lock_path), False

    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(lock_path), flags, 0o644)
    except OSError as error:
        if getattr(error, "errno", None) == errno.EEXIST:
            return _open_existing_regular_lock_fd(lock_path), False
        if getattr(error, "errno", None) == getattr(errno, "ELOOP", object()):
            raise AdoptPublicationLockError(
                f"unsafe symlinked publication lock is unsupported: {lock_path}"
            ) from error
        raise AdoptPublicationLockError(
            f"cannot create publication lock {lock_path}: {error}"
        ) from error
    try:
        _validate_opened_lock_fd(fd, lock_path, None)
    except Exception as error:
        os.close(fd)
        raise AdoptPublicationLockError(
            str(error),
            lock_created=True,
            lock_path=lock_path,
        ) from error
    return fd, True


def _require_lock_backend() -> str:
    backend = _available_lock_backend()
    if backend is None:
        raise AdoptPublicationLockError(
            f"cross-process publication lock is unsupported on {sys.platform!r}; "
            "refusing to create or adopt .publish.lock without an advisory lock"
        )
    return backend


def _doctor_or_raise(
    root: Path,
    indexer: str,
    *,
    allow_unlocked_managed: bool,
    publication_lock_held: bool = False,
    lock_created: bool = False,
    lock_path: Optional[Path] = None,
) -> Dict[str, Any]:
    try:
        if publication_lock_held:
            # Internal package call: the adoption path already holds the
            # exclusive publication lock. Acquiring a nested shared flock is
            # platform-dependent and can release or deadlock the outer lock.
            report = _audit_graph_root_unlocked(root, indexer=indexer)
        else:
            report = audit_graph_root(
                root,
                indexer=indexer,
                allow_unlocked_managed=allow_unlocked_managed,
            )
    except AmbiguousIndexerError:
        raise
    except (PersistedGraphDoctorError, SnapshotGraphAuditError) as error:
        raise AdoptPublicationLockError(
            str(error),
            lock_created=lock_created,
            lock_path=lock_path,
        ) from error
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        raise AdoptPublicationLockError(
            f"unreadable persisted graph: {error}",
            lock_created=lock_created,
            lock_path=lock_path,
        ) from error
    if not report.get("ok"):
        status = report.get("status")
        n_anom = report.get("n_anomalies")
        raise AdoptIntegrityError(
            f"persisted integrity is not ok (status={status} anomalies={n_anom}); "
            "refusing to adopt an invalid, incomplete, or mutated graph",
            lock_created=lock_created,
            lock_path=lock_path,
        )
    resolved = report.get("indexer")
    if resolved not in {"python", "c"}:
        raise AdoptPublicationLockError(
            "doctor did not resolve a concrete indexer",
            lock_created=lock_created,
            lock_path=lock_path,
        )
    snapshot = report.get("snapshot")
    if not isinstance(snapshot, str) or not is_published_snapshot_id(snapshot):
        raise AdoptPublicationLockError(
            "doctor did not resolve a published current snapshot",
            lock_created=lock_created,
            lock_path=lock_path,
        )
    return report


def _observed_payload_fingerprint(report: Dict[str, Any]) -> Dict[str, str]:
    """Return the doctor's observed graph fingerprint excluding the lock."""
    verification = report.get("read_only_verification")
    if not isinstance(verification, dict):
        raise AdoptPublicationLockError(
            "doctor did not return read-only verification"
        )
    fingerprint = verification.get("fingerprint")
    if not isinstance(fingerprint, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in fingerprint.items()
    ):
        raise AdoptPublicationLockError(
            "doctor did not return a valid persisted-input fingerprint"
        )
    return {
        key: value
        for key, value in fingerprint.items()
        if key != "graph/publish_lock"
    }


def _require_locked_path_identity(
    fd: int,
    lock_path: Path,
    *,
    lock_created: bool,
) -> None:
    """Prove the pathname still names the inode held by ``fd``."""
    try:
        _validate_opened_lock_fd(fd, lock_path, os.fstat(fd))
    except (AdoptPublicationLockError, OSError) as error:
        raise AdoptPublicationLockError(
            f"publication lock path no longer names the locked inode: "
            f"{lock_path}",
            lock_created=lock_created,
            lock_path=lock_path,
        ) from error


def _result(
    *,
    status: str,
    graph: Path,
    lock_path: Path,
    lock_created: bool,
    indexer: str,
    snapshot: str,
    payload_unchanged: bool,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "graph": str(graph),
        "lock": str(lock_path),
        "lock_created": bool(lock_created),
        "indexer": indexer,
        "snapshot": snapshot,
        "payload_unchanged": bool(payload_unchanged),
        "offline_assumption": OFFLINE_ASSUMPTION,
    }


def adopt_publication_lock(
    graph: Path,
    indexer: str,
    *,
    offline_confirmed: bool,
) -> Dict[str, Any]:
    """Adopt ``.publish.lock`` on a doctor-valid managed graph.

    Returns the stable result object. Raises :class:`AdoptPublicationLockError`
    (exit 2) or :class:`AdoptIntegrityError` (exit 1). Never deletes a lock
    file after its pathname has been exposed.
    """
    lang = indexer.strip().lower()
    if lang not in {"auto", "python", "c"}:
        raise AdoptPublicationLockError(
            f"unknown --indexer {indexer!r}; use auto, python, or c"
        )
    root = _resolve_graph_root(graph)
    _require_adoptable_managed_graph(root)
    lock_path = root / PUBLICATION_LOCK_NAME
    existing = _inspect_lock_path(lock_path)
    if existing is None and not offline_confirmed:
        raise AdoptPublicationLockError(_QUIESCENCE_MESSAGE.strip())
    if existing is not None and not offline_confirmed:
        raise AdoptPublicationLockError(
            "adoption is an explicit offline-confirmed operator action; "
            "pass --offline-confirmed. This program cannot prove that no "
            "legacy reader or publisher that ignores .publish.lock is active."
        )
    _require_lock_backend()
    preflight = _doctor_or_raise(
        root,
        lang,
        allow_unlocked_managed=True,
    )
    resolved = str(preflight["indexer"])
    before_payload = _observed_payload_fingerprint(preflight)

    fd: Optional[int] = None
    backend: Optional[str] = None
    created = False
    try:
        fd, created = _open_adoption_lock_fd(lock_path)
        try:
            backend = _acquire_exclusive_lock(fd)
        except ByogPublicationLockError as error:
            raise AdoptPublicationLockError(
                f"could not acquire the exclusive publication lock on "
                f"{lock_path}: {error}",
                lock_created=created,
                lock_path=lock_path,
            ) from error
        except OSError as error:
            raise AdoptPublicationLockError(
                f"could not acquire the exclusive publication lock on "
                f"{lock_path}: {error}",
                lock_created=created,
                lock_path=lock_path,
            ) from error
        _require_locked_path_identity(fd, lock_path, lock_created=created)
        post = _doctor_or_raise(
            root,
            resolved,
            allow_unlocked_managed=False,
            publication_lock_held=True,
            lock_created=created,
            lock_path=lock_path,
        )
        # The doctor reads the lock pathname while the descriptor is held.
        # Recheck afterwards so a replacement during the audit is not reported
        # as a successfully adopted locking domain.
        _require_locked_path_identity(fd, lock_path, lock_created=created)
        payload_unchanged = (
            before_payload == _observed_payload_fingerprint(post)
        )
        return _result(
            status="adopted" if created else "already_adopted",
            graph=root,
            lock_path=lock_path,
            lock_created=created,
            indexer=str(post["indexer"]),
            snapshot=str(post["snapshot"]),
            payload_unchanged=payload_unchanged,
        )
    finally:
        if backend is not None and fd is not None:
            try:
                _release_exclusive_lock(fd, backend)
            except OSError:
                pass
        if fd is not None:
            os.close(fd)


def result_to_json(result: Dict[str, Any]) -> str:
    body = {key: result[key] for key in RESULT_KEYS}
    return (
        json.dumps(
            body,
            sort_keys=True,
            indent=2,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    )


def format_result(result: Dict[str, Any]) -> str:
    return (
        f"adopt-publication-lock: {result['status']} "
        f"graph={result['graph']} snapshot={result['snapshot']} "
        f"indexer={result['indexer']} lock_created={str(result['lock_created']).lower()}"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Explicit offline adoption of .publish.lock for a pre-lock managed "
            "BYOG graph. Never an automatic MCP or doctor side effect. Creates "
            "only <graph>/.publish.lock and never reindexes or rewrites payload."
        )
    )
    parser.add_argument(
        "--graph",
        "-g",
        type=Path,
        required=True,
        help="Managed BYOG graph root (current + snapshots/), relative to cwd.",
    )
    parser.add_argument(
        "--indexer",
        type=str,
        required=True,
        choices=("auto", "python", "c"),
        help="python, c, or auto (fail closed if persisted evidence is ambiguous).",
    )
    parser.add_argument(
        "--offline-confirmed",
        action="store_true",
        help=_QUIESCENCE_HELP,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit deterministic machine-readable JSON on stdout",
    )
    args = parser.parse_args(argv)
    try:
        result = adopt_publication_lock(
            args.graph,
            args.indexer,
            offline_confirmed=bool(args.offline_confirmed),
        )
    except AmbiguousIndexerError as error:
        print(f"adopt-publication-lock: {error}", file=sys.stderr)
        return 2
    except AdoptPublicationLockError as error:
        print(f"adopt-publication-lock: {error}", file=sys.stderr)
        if error.lock_created and error.lock_path is not None:
            print(
                f"adopt-publication-lock: .publish.lock was created at "
                f"{error.lock_path} and was not removed.",
                file=sys.stderr,
            )
        return error.exit_code
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"adopt-publication-lock: {error}", file=sys.stderr)
        return 2

    if args.json:
        sys.stdout.write(result_to_json(result))
    else:
        print(format_result(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
