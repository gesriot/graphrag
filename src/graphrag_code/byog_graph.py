"""
Common BYOG graph loader and model (extracted from context_pack and graph_query).

Provides a clean ByogGraph class that both tools can use.

This reduces duplication and makes it easier to add local queries, module packs, etc.

All deterministic, no external API.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# Transitive uses_type closure (consumer-only; never invents graph facts).
TYPE_CLOSURE_DIRECTIONS = frozenset({"dependencies", "users", "both"})
DEFAULT_TYPE_CLOSURE_MAX_DEPTH = 3
DEFAULT_TYPE_CLOSURE_MAX_NODES = 50
DEFAULT_TYPE_CLOSURE_MAX_EDGES = 100

PUBLICATION_LOCK_NAME = ".publish.lock"
STAGING_NAME_PREFIX = ".staging-"
STAGING_WRITER_LOCK_NAME = ".staging-writer.lock"


class ByogPublicationLockError(RuntimeError):
    """Raised when a cross-process publication lock cannot be taken honestly."""


class ByogReaderLockError(RuntimeError):
    """Raised when a shared reader lock on ``.publish.lock`` cannot be taken."""


class StagingWriterLeaseError(RuntimeError):
    """Raised when the private staging-writer lease cannot be used honestly."""


class StagingWriterLockContention(StagingWriterLeaseError):
    """A cooperating process held the exclusive staging-writer lease."""


class StagingWriterLockUnsafe(StagingWriterLeaseError):
    """Writer-lock metadata is missing, replaced, symlinked, or not regular."""


def is_staging_snapshot_name(name: str) -> bool:
    """Return True for a private publisher staging directory name."""
    return bool(name) and name.startswith(STAGING_NAME_PREFIX)


def is_published_snapshot_id(name: str) -> bool:
    """Return True for a final snapshot id the publisher may retain or point at.

    Staging, lock, and other hidden protocol names are not published ids.
    """
    if not name or name in {".", ".."}:
        return False
    if name.startswith(".") or is_staging_snapshot_name(name):
        return False
    if "/" in name or "\\" in name or "\x00" in name:
        return False
    return Path(name).name == name


def _resolve_output_base(base: Path) -> Path:
    """Return the directory containing the active parquets.

    Supports new snapshot layout:
        <base>/
            current          # contains snapshot id
            snapshots/
                <id>/
                    entities.parquet
                    ...
    Falls back to flat structure (old behavior or test tmp dirs) if no 'current' or the snapshot dir is missing.
    """
    base = Path(base)
    current_file = base / "current"
    if current_file.exists():
        try:
            snap_id = current_file.read_text().strip()
            if is_published_snapshot_id(snap_id):
                snap_dir = base / "snapshots" / snap_id
                if snap_dir.exists():
                    return snap_dir
        except Exception:
            pass
    # flat fallback (direct writes in tests, old byog dirs, etc.)
    return base


def _has_core_parquets(base: Path) -> bool:
    return (
        (base / "entities.parquet").exists()
        and (base / "relationships.parquet").exists()
        and (base / "text_units.parquet").exists()
    )


def _resolve_graph_base(root: Path) -> Path:
    """Resolve active parquet base from either root-level snapshots or output/ fallback."""
    root = Path(root)

    root_base = _resolve_output_base(root)
    if _has_core_parquets(root_base):
        return root_base

    out_base = root / "output"
    output_base = _resolve_output_base(out_base)
    if _has_core_parquets(output_base):
        return output_base

    # Keep the historical failure mode: let pandas raise a useful file-not-found
    # against output/ when neither layout exists.
    return output_base


def _atomic_write_parquet(df: pd.DataFrame, final_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=final_path.parent, suffix=".parquet.tmp", delete=False
    ) as tmp:
        tmp_path = Path(tmp.name)
    try:
        table = pa.Table.from_pandas(df)
        pq.write_table(table, tmp_path)
        os.replace(tmp_path, final_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _atomic_write_text(text: str, final_path: Path) -> None:
    final_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=final_path.parent, suffix=".tmp", delete=False, mode="w", encoding="utf-8"
    ) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    try:
        os.replace(tmp_path, final_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _available_lock_backend() -> Optional[str]:
    try:
        import fcntl  # noqa: F401
    except ImportError:
        fcntl = None
    else:
        return "fcntl"
    try:
        import msvcrt  # noqa: F401
    except ImportError:
        return None
    return "msvcrt"


def _windows_overlapped():
    import ctypes
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    return Overlapped()


def _lock_file_ex(fd: int, *, exclusive: bool, nonblocking: bool = False) -> None:
    """Shared/exclusive LockFileEx so Windows readers and publishers interoperate.

    ``msvcrt.locking`` has no shared mode. Exclusive and shared must use the
    same kernel API on the same byte range.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lock_ex = kernel32.LockFileEx
    lock_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    lock_ex.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    flags = 0x00000002 if exclusive else 0  # LOCKFILE_EXCLUSIVE_LOCK
    if nonblocking:
        flags |= 0x00000001  # LOCKFILE_FAIL_IMMEDIATELY
    overlapped = _windows_overlapped()
    if not lock_ex(handle, flags, 0, 1, 0, ctypes.byref(overlapped)):
        err = ctypes.get_last_error()
        raise OSError(err, f"LockFileEx failed with Windows error {err}")


def _unlock_file_ex(fd: int) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    unlock_ex = kernel32.UnlockFileEx
    unlock_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    unlock_ex.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(fd)
    overlapped = _windows_overlapped()
    if not unlock_ex(handle, 0, 1, 0, ctypes.byref(overlapped)):
        err = ctypes.get_last_error()
        raise OSError(err, f"UnlockFileEx failed with Windows error {err}")


def _acquire_lock(fd: int, *, exclusive: bool) -> str:
    backend = _available_lock_backend()
    if backend == "fcntl":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        return backend
    if backend == "msvcrt":
        _lock_file_ex(fd, exclusive=exclusive)
        return backend
    raise ByogPublicationLockError(
        f"cross-process publication lock is unsupported on {sys.platform!r}; "
        "refusing to lock snapshots without an advisory lock"
    )


def _acquire_exclusive_lock(fd: int) -> str:
    return _acquire_lock(fd, exclusive=True)


def _try_acquire_exclusive_lock(fd: int) -> str:
    """Nonblocking exclusive acquire. Contention is not malformed lock state."""
    backend = _available_lock_backend()
    if backend == "fcntl":
        import errno
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StagingWriterLockContention(
                "staging writer lease is held by a cooperating process"
            ) from error
        except OSError as error:
            if error.errno in {
                errno.EAGAIN,
                errno.EACCES,
                getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
            }:
                raise StagingWriterLockContention(
                    "staging writer lease is held by a cooperating process"
                ) from error
            raise
        return backend
    if backend == "msvcrt":
        try:
            _lock_file_ex(fd, exclusive=True, nonblocking=True)
        except OSError as error:
            # ERROR_LOCK_VIOLATION=33, ERROR_IO_PENDING=997, ERROR_LOCK_FAILED=167
            if getattr(error, "errno", None) in {33, 167, 997}:
                raise StagingWriterLockContention(
                    "staging writer lease is held by a cooperating process"
                ) from error
            raise
        return backend
    raise StagingWriterLeaseError(
        f"nonblocking exclusive staging-writer lock is unsupported on "
        f"{sys.platform!r}; refusing to guess writer-lease state"
    )


def _release_lock(fd: int, backend: str) -> None:
    if backend == "fcntl":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if backend == "msvcrt":
        _unlock_file_ex(fd)


def _release_exclusive_lock(fd: int, backend: str) -> None:
    _release_lock(fd, backend)


def is_managed_snapshot_layout(path: Path) -> bool:
    """Return True for a graph root with ``current`` and ``snapshots/``."""
    try:
        return _validate_managed_snapshot_layout(Path(path))
    except ByogReaderLockError:
        return False


def _validate_managed_snapshot_layout(root: Path) -> bool:
    """Return False for a flat layout; reject incomplete or unsafe managed markers."""
    import stat as stat_mod

    root = Path(root)
    current = root / "current"
    snapshots = root / "snapshots"

    def inspect(marker: Path):
        try:
            return marker.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise ByogReaderLockError(
                f"cannot inspect managed graph marker {marker}: {error}"
            ) from error

    current_stat = inspect(current)
    snapshots_stat = inspect(snapshots)
    if current_stat is None and snapshots_stat is None:
        return False
    if current_stat is not None and stat_mod.S_ISLNK(current_stat.st_mode):
        raise ByogReaderLockError(
            f"unsafe symlinked current pointer is unsupported: {current}"
        )
    if snapshots_stat is not None and stat_mod.S_ISLNK(snapshots_stat.st_mode):
        raise ByogReaderLockError(
            f"unsafe symlinked snapshots directory is unsupported: {snapshots}"
        )
    if current_stat is None or snapshots_stat is None:
        raise ByogReaderLockError(
            f"incomplete managed snapshot layout under {root}: "
            "both current and snapshots/ are required"
        )
    if not stat_mod.S_ISREG(current_stat.st_mode):
        raise ByogReaderLockError(f"current pointer is not a regular file: {current}")
    if not stat_mod.S_ISDIR(snapshots_stat.st_mode):
        raise ByogReaderLockError(
            f"snapshots path is not a real directory: {snapshots}"
        )
    return True


def _open_exclusive_lock_fd(lock_path: Path) -> int:
    import stat as stat_mod

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    before = None
    try:
        before = lock_path.lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ByogPublicationLockError(
            f"cannot inspect publication lock {lock_path}: {error}"
        ) from error
    if before is not None:
        if stat_mod.S_ISLNK(before.st_mode):
            raise ByogPublicationLockError(
                f"unsafe symlinked publication lock is unsupported: {lock_path}"
            )
        if not stat_mod.S_ISREG(before.st_mode):
            raise ByogPublicationLockError(
                f"publication lock is not a regular file: {lock_path}"
            )
    open_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(lock_path), open_flags, 0o644)
    except OSError as error:
        raise ByogPublicationLockError(
            f"cannot open publication lock {lock_path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        if not stat_mod.S_ISREG(opened.st_mode):
            raise ByogPublicationLockError(
                f"publication lock is not a regular file: {lock_path}"
            )
        if before is not None and (
            before.st_dev != opened.st_dev or before.st_ino != opened.st_ino
        ):
            raise ByogPublicationLockError(
                f"publication lock changed while opening it: {lock_path}"
            )
        current = lock_path.lstat()
        if stat_mod.S_ISLNK(current.st_mode):
            raise ByogPublicationLockError(
                f"unsafe symlinked publication lock is unsupported: {lock_path}"
            )
        if (
            not stat_mod.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise ByogPublicationLockError(
                f"publication lock changed while opening it: {lock_path}"
            )
    except Exception:
        os.close(fd)
        raise
    return fd


def _open_shared_lock_fd(lock_path: Path) -> int:
    """Open an existing regular lock file for reading. Never create or write."""
    import errno
    import stat as stat_mod

    try:
        before = lock_path.lstat()
    except FileNotFoundError as error:
        raise ByogReaderLockError(
            f"publication lock is missing; refusing to read a managed snapshot graph: {lock_path}"
        ) from error
    except OSError as error:
        raise ByogReaderLockError(
            f"cannot inspect publication lock {lock_path}: {error}"
        ) from error
    if stat_mod.S_ISLNK(before.st_mode):
        raise ByogReaderLockError(
            f"unsafe symlinked publication lock is unsupported: {lock_path}"
        )
    if not stat_mod.S_ISREG(before.st_mode):
        raise ByogReaderLockError(
            f"publication lock is not a regular file: {lock_path}"
        )
    open_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(lock_path), open_flags)
    except OSError as error:
        if getattr(error, "errno", None) == getattr(errno, "ELOOP", object()):
            raise ByogReaderLockError(
                f"unsafe symlinked publication lock is unsupported: {lock_path}"
            ) from error
        raise ByogReaderLockError(
            f"cannot open publication lock {lock_path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        if not stat_mod.S_ISREG(opened.st_mode):
            raise ByogReaderLockError(
                f"publication lock is not a regular file: {lock_path}"
            )
        if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
            raise ByogReaderLockError(
                f"publication lock changed while opening it: {lock_path}"
            )
        current = lock_path.lstat()
        if stat_mod.S_ISLNK(current.st_mode):
            raise ByogReaderLockError(
                f"unsafe symlinked publication lock is unsupported: {lock_path}"
            )
        if (
            not stat_mod.S_ISREG(current.st_mode)
            or current.st_dev != opened.st_dev
            or current.st_ino != opened.st_ino
        ):
            raise ByogReaderLockError(
                f"publication lock changed while opening it: {lock_path}"
            )
    except Exception:
        os.close(fd)
        raise
    return fd


@contextmanager
def _publication_lock(out_root: Path) -> Iterator[None]:
    """Exclusive cross-process lock for snapshot promotion, current, and retention."""
    lock_path = Path(out_root) / PUBLICATION_LOCK_NAME
    fd = _open_exclusive_lock_fd(lock_path)
    backend: Optional[str] = None
    try:
        backend = _acquire_exclusive_lock(fd)
        _validate_existing_exclusive_lock_fd(fd, lock_path)
        yield
    finally:
        if backend is not None:
            try:
                _release_exclusive_lock(fd, backend)
            except OSError:
                pass
        os.close(fd)


@contextmanager
def _shared_publication_lock(graph_root: Path) -> Iterator[None]:
    """Shared reader lock. Never creates or rewrites ``.publish.lock``."""
    lock_path = Path(graph_root) / PUBLICATION_LOCK_NAME
    fd = _open_shared_lock_fd(lock_path)
    backend: Optional[str] = None
    try:
        try:
            backend = _acquire_lock(fd, exclusive=False)
        except ByogPublicationLockError as error:
            raise ByogReaderLockError(str(error)) from error
        except OSError as error:
            raise ByogReaderLockError(
                f"cannot acquire shared publication lock {lock_path}: {error}"
            ) from error
        try:
            _validate_existing_exclusive_lock_fd(fd, lock_path)
        except ByogPublicationLockError as error:
            raise ByogReaderLockError(str(error)) from error
        yield
    finally:
        if backend is not None:
            try:
                _release_lock(fd, backend)
            except OSError:
                pass
        os.close(fd)


@contextmanager
def graph_read_lease(
    graph_root: Path,
    *,
    allow_unlocked_managed: bool = False,
) -> Iterator[None]:
    """Hold a shared advisory lock for one logical read of a managed graph.

    For a managed ``current + snapshots/`` layout with an existing
    ``.publish.lock`` this acquires a shared lock before the caller should
    resolve ``current``. Cooperating publishers and retention take the same
    lock exclusively, so they wait until this block exits. Concurrent readers
    may share the lock. Process death releases it.

    A managed layout without ``.publish.lock`` fails closed by default: a
    future publisher could otherwise create the lock and retain snapshots
    underneath an unprotected reader. ``allow_unlocked_managed`` exists only
    for explicit compatibility reads of immutable pre-lock evidence graphs;
    it never creates the missing file and provides no retention guarantee.

    For a legacy flat-parquet directory this is also a no-op.

    This is not a distributed lease service and does not protect against
    tools that ignore ``.publish.lock``.
    """
    root = Path(graph_root)
    if not _validate_managed_snapshot_layout(root):
        yield
        return
    lock_path = root / PUBLICATION_LOCK_NAME
    try:
        lock_path.lstat()
    except FileNotFoundError:
        if allow_unlocked_managed:
            yield
            return
        raise ByogReaderLockError(
            f"publication lock is missing; refusing an unleased managed graph read: "
            f"{lock_path}"
        )
    except OSError as error:
        raise ByogReaderLockError(
            f"cannot inspect publication lock {lock_path}: {error}"
        ) from error
    with _shared_publication_lock(root):
        yield


def _validate_existing_exclusive_lock_fd(
    fd: int,
    lock_path: Path,
    *,
    expected: Optional[os.stat_result] = None,
) -> None:
    """Prove ``lock_path`` still names the regular file open as ``fd``."""
    import stat as stat_mod

    try:
        opened = os.fstat(fd)
    except OSError as error:
        raise ByogPublicationLockError(
            f"cannot inspect opened publication lock {lock_path}: {error}"
        ) from error
    if not stat_mod.S_ISREG(opened.st_mode):
        raise ByogPublicationLockError(
            f"publication lock is not a regular file: {lock_path}"
        )
    if expected is not None and (
        opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino
    ):
        raise ByogPublicationLockError(
            f"publication lock changed while opening it: {lock_path}"
        )
    try:
        current = lock_path.lstat()
    except FileNotFoundError as error:
        raise ByogPublicationLockError(
            f"publication lock disappeared while acquiring it: {lock_path}"
        ) from error
    except OSError as error:
        raise ByogPublicationLockError(
            f"cannot re-inspect publication lock {lock_path}: {error}"
        ) from error
    if stat_mod.S_ISLNK(current.st_mode):
        raise ByogPublicationLockError(
            f"unsafe symlinked publication lock is unsupported: {lock_path}"
        )
    if (
        not stat_mod.S_ISREG(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        raise ByogPublicationLockError(
            f"publication lock changed while acquiring it: {lock_path}"
        )


def _open_existing_exclusive_lock_fd(lock_path: Path) -> int:
    """Open an existing regular lock for exclusive use. Never create or write.

    Publishers that must create ``.publish.lock`` still use
    ``_open_exclusive_lock_fd``. Activation and other existing-lock
    mutations must not create, truncate, chmod, or replace the file.
    """
    import errno
    import stat as stat_mod

    try:
        before = lock_path.lstat()
    except FileNotFoundError as error:
        raise ByogPublicationLockError(
            f"publication lock is missing; refusing an exclusive mutation of "
            f"an unleased managed graph: {lock_path}"
        ) from error
    except OSError as error:
        raise ByogPublicationLockError(
            f"cannot inspect publication lock {lock_path}: {error}"
        ) from error
    if stat_mod.S_ISLNK(before.st_mode):
        raise ByogPublicationLockError(
            f"unsafe symlinked publication lock is unsupported: {lock_path}"
        )
    if not stat_mod.S_ISREG(before.st_mode):
        raise ByogPublicationLockError(
            f"publication lock is not a regular file: {lock_path}"
        )
    # Advisory locking does not require permission to modify the lock bytes.
    # Opening read-only makes the "never writes the existing lock" contract
    # enforceable by the descriptor itself (and still supports flock /
    # LockFileEx on the supported backends).
    open_flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(lock_path), open_flags)
    except OSError as error:
        if getattr(error, "errno", None) == getattr(errno, "ELOOP", object()):
            raise ByogPublicationLockError(
                f"unsafe symlinked publication lock is unsupported: {lock_path}"
            ) from error
        if getattr(error, "errno", None) == errno.ENOENT:
            raise ByogPublicationLockError(
                f"publication lock is missing; refusing an exclusive mutation of "
                f"an unleased managed graph: {lock_path}"
            ) from error
        raise ByogPublicationLockError(
            f"cannot open publication lock {lock_path}: {error}"
        ) from error
    try:
        _validate_existing_exclusive_lock_fd(fd, lock_path, expected=before)
    except Exception:
        os.close(fd)
        raise
    return fd


@contextmanager
def graph_exclusive_lease(graph_root: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on an existing regular ``.publish.lock``.

    For a managed ``current + snapshots/`` graph this acquires the same
    exclusive lock publishers and retention use, but it never creates,
    truncates, rewrites, chmods, or replaces the lock file. Missing,
    symlinked, non-regular, disappearing, or inode-swapped locks fail
    closed. A legacy flat-parquet directory is rejected because there is
    no managed publication protocol to mutate.

    Callers must not acquire a nested shared lease while this block is
    held. Process death releases the lock. This is not a distributed
    lease service and does not protect against tools that ignore
    ``.publish.lock``.
    """
    root = Path(graph_root)
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        raise ByogPublicationLockError(str(error)) from error
    if not managed:
        raise ByogPublicationLockError(
            "exclusive existing-lock lease requires a managed "
            f"current + snapshots/ graph: {root}"
        )
    lock_path = root / PUBLICATION_LOCK_NAME
    fd = _open_existing_exclusive_lock_fd(lock_path)
    backend: Optional[str] = None
    try:
        try:
            backend = _acquire_exclusive_lock(fd)
        except ByogPublicationLockError:
            raise
        except OSError as error:
            raise ByogPublicationLockError(
                f"cannot acquire exclusive publication lock {lock_path}: {error}"
            ) from error
        # Opening and validating precedes the potentially blocking lock call.
        # A lock-ignoring actor could replace the pathname while this process
        # waits, splitting the locking domain unless the held fd is checked
        # against the pathname again after acquisition.
        _validate_existing_exclusive_lock_fd(fd, lock_path)
        yield
    finally:
        if backend is not None:
            try:
                _release_exclusive_lock(fd, backend)
            except OSError:
                pass
        os.close(fd)


def graph_lease_order_key(
    canonical: Path, identity: Tuple[int, int]
) -> Tuple[bytes, int, int]:
    """Global two-graph lease order, independent of source/target role.

    Primary key: canonical UTF-8 path bytes of the real graph root.
    Tie-breaker: ``(st_dev, st_ino)``. Source-shared / target-exclusive
    transfer apply uses this same order so opposing A→B and B→A
    operations cannot create a lock cycle.
    """
    return (str(canonical).encode("utf-8"), int(identity[0]), int(identity[1]))


def ordered_graph_lease_pair(
    left: Path,
    left_identity: Tuple[int, int],
    right: Path,
    right_identity: Tuple[int, int],
) -> Tuple[Path, Path]:
    """Return ``(first, second)`` in the documented global lease order."""
    left_key = graph_lease_order_key(left, left_identity)
    right_key = graph_lease_order_key(right, right_identity)
    if left_key <= right_key:
        return left, right
    return right, left


def _after_graph_mixed_lease_one_held(_root: Path, _exclusive: bool) -> None:
    """Test hook after one mixed-mode lock is held and before the next acquire."""
    return None


def _after_graph_shared_lease_one_held(_root: Path) -> None:
    """Test hook after one shared pair lock is held before the next acquire."""
    return None


def _root_lease_inode(root: Path) -> Tuple[int, int]:
    import stat as stat_mod

    try:
        info = Path(root).lstat()
    except FileNotFoundError as error:
        raise ByogPublicationLockError(
            f"graph root disappeared before mixed-mode lease acquisition: {root}"
        ) from error
    except OSError as error:
        raise ByogPublicationLockError(
            f"cannot inspect graph root {root}: {error}"
        ) from error
    if stat_mod.S_ISLNK(info.st_mode) or not stat_mod.S_ISDIR(info.st_mode):
        raise ByogPublicationLockError(
            f"graph root is not a real directory: {root}"
        )
    return (info.st_dev, info.st_ino)


def _require_root_lease_inode(
    root: Path, expected: Tuple[int, int]
) -> None:
    observed = _root_lease_inode(root)
    if observed != expected:
        raise ByogPublicationLockError(
            "graph root changed during mixed-mode lease acquisition: "
            f"{root}"
        )


def _require_managed_for_mixed_lease(root: Path, *, exclusive: bool) -> None:
    try:
        managed = _validate_managed_snapshot_layout(root)
    except ByogReaderLockError as error:
        if exclusive:
            raise ByogPublicationLockError(str(error)) from error
        raise
    if managed:
        return
    if exclusive:
        raise ByogPublicationLockError(
            "exclusive existing-lock lease requires a managed "
            f"current + snapshots/ graph: {root}"
        )
    raise ByogReaderLockError(
        "legacy flat-parquet directory has no retained snapshot history: "
        f"{root}"
    )


def _open_mixed_lock_fd(lock_path: Path, *, exclusive: bool) -> int:
    if exclusive:
        return _open_existing_exclusive_lock_fd(lock_path)
    return _open_shared_lock_fd(lock_path)


def _acquire_mixed_lock(fd: int, lock_path: Path, *, exclusive: bool) -> str:
    try:
        backend = _acquire_lock(fd, exclusive=exclusive)
    except ByogPublicationLockError as error:
        if exclusive:
            raise
        raise ByogReaderLockError(str(error)) from error
    except OSError as error:
        if exclusive:
            raise ByogPublicationLockError(
                f"cannot acquire exclusive publication lock {lock_path}: {error}"
            ) from error
        raise ByogReaderLockError(
            f"cannot acquire shared publication lock {lock_path}: {error}"
        ) from error
    try:
        _validate_existing_exclusive_lock_fd(fd, lock_path)
    except ByogPublicationLockError as error:
        if exclusive:
            raise
        raise ByogReaderLockError(str(error)) from error
    return backend


@contextmanager
def graph_source_shared_target_exclusive_leases(
    source_root: Path,
    target_root: Path,
) -> Iterator[None]:
    """Hold source-shared and target-exclusive existing-lock leases.

    Both ``.publish.lock`` files are opened read-only without following
    symlinks and are never created, truncated, chmodded, written, or
    replaced. Shared vs exclusive mode is assigned by graph role, not
    by acquisition order. The two held descriptors are flocked in the
    global two-graph order (canonical UTF-8 path bytes, then
    ``(st_dev, st_ino)``). This is not a lock upgrade and does not nest
    ``graph_read_lease`` inside ``graph_exclusive_lease``.

    Both lock identities are opened and bound before either potentially
    blocking acquisition. After each acquisition its pathname/inode and
    graph-root identity are revalidated, and both are revalidated after
    both are held. Release is reverse acquisition order. Failure to
    acquire or revalidate the second lock releases the first.
    """
    source = Path(source_root)
    target = Path(target_root)
    _require_managed_for_mixed_lease(source, exclusive=False)
    _require_managed_for_mixed_lease(target, exclusive=True)
    source_inode = _root_lease_inode(source)
    target_inode = _root_lease_inode(target)
    if source_inode == target_inode:
        raise ByogPublicationLockError(
            "source-graph and target-graph must be different directory "
            f"identities: {source} and {target}"
        )
    ordered = [
        (source, source_inode, False),
        (target, target_inode, True),
    ]
    ordered.sort(key=lambda item: graph_lease_order_key(item[0], item[1]))
    held: List[
        Tuple[int, Optional[str], Path, Path, Tuple[int, int], bool]
    ] = []
    try:
        # Bind both existing lock identities before either potentially
        # blocking acquisition. Otherwise the second pathname could be
        # replaced while the first acquisition waits, silently moving this
        # operation into a different lock domain.
        for root, root_inode, exclusive in ordered:
            _require_root_lease_inode(root, root_inode)
            lock_path = root / PUBLICATION_LOCK_NAME
            fd = _open_mixed_lock_fd(lock_path, exclusive=exclusive)
            held.append(
                (fd, None, lock_path, root, root_inode, exclusive)
            )
        for index, (
            fd,
            _backend,
            lock_path,
            root,
            root_inode,
            exclusive,
        ) in enumerate(held):
            try:
                backend = _acquire_mixed_lock(fd, lock_path, exclusive=exclusive)
            except Exception:
                raise
            held[index] = (
                fd,
                backend,
                lock_path,
                root,
                root_inode,
                exclusive,
            )
            _require_root_lease_inode(root, root_inode)
            _after_graph_mixed_lease_one_held(root, exclusive)
        for fd, _backend, lock_path, root, root_inode, exclusive in held:
            try:
                _validate_existing_exclusive_lock_fd(fd, lock_path)
            except ByogPublicationLockError as error:
                if exclusive:
                    raise
                raise ByogReaderLockError(str(error)) from error
            _require_root_lease_inode(root, root_inode)
        yield
    finally:
        for (
            fd,
            backend,
            _lock_path,
            _root,
            _root_inode,
            _exclusive,
        ) in reversed(held):
            if backend is not None:
                try:
                    _release_lock(fd, backend)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass


@contextmanager
def graph_shared_leases(
    left_root: Path,
    right_root: Path,
) -> Iterator[None]:
    """Hold two existing publication locks in shared mode.

    Both managed graph roots and both regular ``.publish.lock`` identities
    are bound before either potentially blocking acquisition. The locks are
    acquired in the global two-graph order, independent of caller role. Each
    lock pathname and graph-root identity is revalidated after acquisition
    and again after both locks are held. Release is in reverse order.

    This prevents a replacement of the not-yet-acquired second lock while
    the first acquisition waits from silently moving a two-graph read into a
    different advisory-lock domain.
    """
    left = Path(left_root)
    right = Path(right_root)
    _require_managed_for_mixed_lease(left, exclusive=False)
    _require_managed_for_mixed_lease(right, exclusive=False)
    left_inode = _root_lease_inode(left)
    right_inode = _root_lease_inode(right)
    if left_inode == right_inode:
        raise ByogReaderLockError(
            "shared graph lease pair requires different directory identities: "
            f"{left} and {right}"
        )
    ordered = [(left, left_inode), (right, right_inode)]
    ordered.sort(key=lambda item: graph_lease_order_key(item[0], item[1]))
    held: List[Tuple[int, Optional[str], Path, Path, Tuple[int, int]]] = []
    try:
        for root, root_inode in ordered:
            _require_root_lease_inode(root, root_inode)
            lock_path = root / PUBLICATION_LOCK_NAME
            fd = _open_shared_lock_fd(lock_path)
            held.append((fd, None, lock_path, root, root_inode))
        for index, (fd, _backend, lock_path, root, root_inode) in enumerate(held):
            backend = _acquire_mixed_lock(fd, lock_path, exclusive=False)
            held[index] = (fd, backend, lock_path, root, root_inode)
            _require_root_lease_inode(root, root_inode)
            _after_graph_shared_lease_one_held(root)
        for fd, _backend, lock_path, root, root_inode in held:
            try:
                _validate_existing_exclusive_lock_fd(fd, lock_path)
            except ByogPublicationLockError as error:
                raise ByogReaderLockError(str(error)) from error
            _require_root_lease_inode(root, root_inode)
        yield
    finally:
        for fd, backend, _lock_path, _root, _root_inode in reversed(held):
            if backend is not None:
                try:
                    _release_lock(fd, backend)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass


def _remove_staging_dir(stage_dir: Path) -> None:
    if not stage_dir.exists():
        return
    if not is_staging_snapshot_name(stage_dir.name):
        return
    shutil.rmtree(stage_dir)


def _staging_writer_lock_path(stage_dir: Path) -> Path:
    return Path(stage_dir) / STAGING_WRITER_LOCK_NAME


def _raise_staging_writer_lock_unsafe(lock_path: Path, reason: str) -> None:
    raise StagingWriterLockUnsafe(f"{reason}: {lock_path}")


def _validate_staging_writer_lock_fd(
    fd: int,
    lock_path: Path,
    *,
    expected: Optional[os.stat_result] = None,
) -> os.stat_result:
    """Prove ``lock_path`` still names the regular file open as ``fd``."""
    import stat as stat_mod

    try:
        opened = os.fstat(fd)
    except OSError as error:
        raise StagingWriterLockUnsafe(
            f"cannot inspect opened staging writer lock {lock_path}: {error}"
        ) from error
    if not stat_mod.S_ISREG(opened.st_mode):
        _raise_staging_writer_lock_unsafe(
            lock_path, "staging writer lock is not a regular file"
        )
    if expected is not None and (
        opened.st_dev != expected.st_dev or opened.st_ino != expected.st_ino
    ):
        _raise_staging_writer_lock_unsafe(
            lock_path, "staging writer lock changed while opening it"
        )
    try:
        current = lock_path.lstat()
    except FileNotFoundError as error:
        raise StagingWriterLockUnsafe(
            f"staging writer lock disappeared while opening it: {lock_path}"
        ) from error
    except OSError as error:
        raise StagingWriterLockUnsafe(
            f"cannot re-inspect staging writer lock {lock_path}: {error}"
        ) from error
    if stat_mod.S_ISLNK(current.st_mode):
        _raise_staging_writer_lock_unsafe(
            lock_path, "unsafe symlinked staging writer lock is unsupported"
        )
    if (
        not stat_mod.S_ISREG(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        _raise_staging_writer_lock_unsafe(
            lock_path, "staging writer lock changed while opening it"
        )
    return opened


def _inspect_staging_writer_lock_path(lock_path: Path) -> Optional[os.stat_result]:
    import stat as stat_mod

    try:
        info = lock_path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise StagingWriterLockUnsafe(
            f"cannot inspect staging writer lock {lock_path}: {error}"
        ) from error
    if stat_mod.S_ISLNK(info.st_mode):
        _raise_staging_writer_lock_unsafe(
            lock_path, "unsafe symlinked staging writer lock is unsupported"
        )
    if not stat_mod.S_ISREG(info.st_mode):
        _raise_staging_writer_lock_unsafe(
            lock_path, "staging writer lock is not a regular file"
        )
    return info


def _has_o_nofollow() -> bool:
    """Test hook. Existing-lock opens use O_NOFOLLOW when this is true."""
    return hasattr(os, "O_NOFOLLOW")


def _open_existing_staging_writer_lock_fd(lock_path: Path) -> Tuple[int, os.stat_result]:
    """Open an existing regular writer lock. Never create, write, or follow."""
    import errno
    import stat as stat_mod

    before = _inspect_staging_writer_lock_path(lock_path)
    if before is None:
        _raise_staging_writer_lock_unsafe(
            lock_path, "staging writer lock disappeared while opening it"
        )
        raise AssertionError("unreachable")
    # O_NOFOLLOW is unavailable on Windows. The pre-open lstat plus the
    # post-open fd/path identity check still rejects a raced reparse/symlink
    # before any advisory lock is attempted, and this descriptor is read-only.
    open_flags = os.O_RDONLY
    if _has_o_nofollow():
        open_flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(lock_path), open_flags)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT}:
            raise StagingWriterLockUnsafe(
                f"staging writer lock changed or became unsafe while opening "
                f"it: {lock_path}"
            ) from error
        raise StagingWriterLeaseError(
            f"cannot open staging writer lock {lock_path}: {error}"
        ) from error
    try:
        opened = _validate_staging_writer_lock_fd(fd, lock_path, expected=before)
        if not stat_mod.S_ISREG(opened.st_mode):
            _raise_staging_writer_lock_unsafe(
                lock_path, "staging writer lock is not a regular file"
            )
        return fd, opened
    except Exception:
        os.close(fd)
        raise


def _open_or_create_staging_writer_lock_fd(
    lock_path: Path, *, must_create: bool = False
) -> Tuple[int, os.stat_result]:
    """Create a regular writer lock if needed. Never truncate, follow, or chmod."""
    import errno
    import stat as stat_mod

    before = None if must_create else _inspect_staging_writer_lock_path(lock_path)
    # Creation needs a writable descriptor, but an already-persistent lock is
    # opened read-only because advisory locking never changes its bytes. This
    # also makes the no-O_NOFOLLOW fallback non-mutating before identity is
    # revalidated on platforms such as Windows.
    open_flags = os.O_RDONLY if before is not None else os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    if before is None:
        # A publisher owns a newly-created private staging directory. Use an
        # exclusive create so a lock-ignoring actor cannot insert a pathname
        # between lstat() and open() and have the publisher silently adopt it.
        open_flags |= os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        open_flags |= os.O_CLOEXEC
    try:
        fd = os.open(str(lock_path), open_flags, 0o644)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EEXIST, errno.ENOENT}:
            raise StagingWriterLockUnsafe(
                f"staging writer lock changed or became unsafe while opening "
                f"it: {lock_path}"
            ) from error
        raise StagingWriterLeaseError(
            f"cannot create staging writer lock {lock_path}: {error}"
        ) from error
    try:
        opened = os.fstat(fd)
        if not stat_mod.S_ISREG(opened.st_mode):
            _raise_staging_writer_lock_unsafe(
                lock_path, "staging writer lock is not a regular file"
            )
        if before is not None and (
            before.st_dev != opened.st_dev or before.st_ino != opened.st_ino
        ):
            _raise_staging_writer_lock_unsafe(
                lock_path, "staging writer lock changed while opening it"
            )
        _validate_staging_writer_lock_fd(fd, lock_path, expected=opened)
        return fd, opened
    except Exception:
        os.close(fd)
        raise


class _HeldStagingWriterLease:
    """Exclusive lease on ``.staging-writer.lock`` for one publisher."""

    def __init__(
        self,
        fd: int,
        backend: str,
        lock_path: Path,
        identity: Tuple[int, int],
    ) -> None:
        self._fd = fd
        self._backend = backend
        self._lock_path = lock_path
        self._identity = identity
        self._closed = False

    @property
    def inode_identity(self) -> Tuple[int, int]:
        """Identity of the regular protocol file held by this lease."""
        return self._identity

    def release_and_remove(self) -> None:
        """Drop the kernel lease and unlink this publisher's lock metadata."""
        self.close()
        try:
            current = self._lock_path.lstat()
        except FileNotFoundError as error:
            raise StagingWriterLockUnsafe(
                f"staging writer lock disappeared before promotion: "
                f"{self._lock_path}"
            ) from error
        except OSError as error:
            raise StagingWriterLockUnsafe(
                f"cannot inspect staging writer lock before removal "
                f"{self._lock_path}: {error}"
            ) from error
        import stat as stat_mod

        if stat_mod.S_ISLNK(current.st_mode):
            _raise_staging_writer_lock_unsafe(
                self._lock_path,
                "unsafe symlinked staging writer lock is unsupported",
            )
        if (
            not stat_mod.S_ISREG(current.st_mode)
            or (current.st_dev, current.st_ino) != self._identity
        ):
            _raise_staging_writer_lock_unsafe(
                self._lock_path,
                "staging writer lock changed before promotion",
            )
        try:
            self._lock_path.unlink()
        except FileNotFoundError as error:
            raise StagingWriterLockUnsafe(
                f"staging writer lock disappeared before removal: "
                f"{self._lock_path}"
            ) from error
        except OSError as error:
            raise StagingWriterLeaseError(
                f"cannot remove staging writer lock {self._lock_path}: {error}"
            ) from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            _release_lock(self._fd, self._backend)
        except OSError:
            pass
        os.close(self._fd)


def _after_staging_writer_lease(stage_dir: Path) -> None:
    """Test hook. Called after the exclusive staging-writer lease is held."""
    return


@contextmanager
def _staging_writer_acquisition_gate(stage_dir: Path) -> Iterator[bool]:
    """Briefly join the managed graph lock domain while acquiring a lease.

    Cleanup holds the graph lock exclusively but must release a claimed writer
    lock before unlinking it on Windows. Without this shared acquisition gate,
    a cooperating writer already waiting on that lock could acquire it in the
    release/unlink window. Existing-lock acquisition is nonblocking while the
    graph gate is held, avoiding a writer-lock/graph-lock deadlock. The gate
    ends as soon as the private writer lease is held, so payload construction
    remains concurrent.

    Standalone staging directories and an initializing graph without a
    complete managed layout keep the historical writer-lock-only behavior.
    """
    stage_dir = Path(stage_dir)
    snapshots_dir = stage_dir.parent
    if snapshots_dir.name != "snapshots":
        yield False
        return
    graph_root = snapshots_dir.parent
    # The first publication has created snapshots/ and its private stage but
    # has not published current yet. That is the expected initializing layout,
    # not a managed graph whose acquisition needs the shared gate.
    try:
        (graph_root / "current").lstat()
    except FileNotFoundError:
        yield False
        return
    except OSError as error:
        raise StagingWriterLeaseError(
            f"cannot inspect graph current marker before acquiring staging "
            f"writer lease {stage_dir}: {error}"
        ) from error
    try:
        (graph_root / PUBLICATION_LOCK_NAME).lstat()
    except FileNotFoundError:
        # Managed graphs published before the lock protocol retain the
        # historical publisher path which creates .publish.lock at promotion.
        # Destructive cleanup refuses such a graph, so there is no cleanup
        # release/unlink window to guard yet.
        yield False
        return
    except OSError as error:
        raise StagingWriterLeaseError(
            f"cannot inspect graph publication lock before acquiring staging "
            f"writer lease {stage_dir}: {error}"
        ) from error
    try:
        managed = _validate_managed_snapshot_layout(graph_root)
    except ByogReaderLockError as error:
        raise StagingWriterLeaseError(
            f"cannot join managed graph lock domain before acquiring staging "
            f"writer lease {stage_dir}: {error}"
        ) from error
    if not managed:
        yield False
        return
    try:
        with graph_read_lease(graph_root, allow_unlocked_managed=False):
            yield True
    except ByogReaderLockError as error:
        raise StagingWriterLeaseError(
            f"cannot acquire managed graph gate for staging writer lease "
            f"{stage_dir}: {error}"
        ) from error


@contextmanager
def staging_writer_lease(stage_dir: Path) -> Iterator[_HeldStagingWriterLease]:
    """Hold the exclusive private writer lease for one staging directory.

    Creates a regular ``.staging-writer.lock`` when absent, then takes a
    blocking exclusive advisory lease. Reacquiring existing writer-lock
    metadata inside an already-managed graph is gated by a short shared graph
    lease and is nonblocking while gated. Fresh publisher lock creation is not
    gated. The shared lease is released before payload construction; it only
    prevents an exclusive cleanup from releasing and removing the writer-lock
    pathname while another cooperating writer is waiting to acquire it.

    The persistent file is protocol metadata, not proof of ownership.
    Process death releases the kernel lease and may leave the file behind.
    The writer lease itself is not a graph-root lease.
    """
    stage_dir = Path(stage_dir)
    if not is_staging_snapshot_name(stage_dir.name):
        raise StagingWriterLeaseError(
            f"staging writer lease requires a private staging directory: "
            f"{stage_dir}"
        )
    lock_path = _staging_writer_lock_path(stage_dir)
    fd: Optional[int] = None
    backend: Optional[str] = None
    held: Optional[_HeldStagingWriterLease] = None
    try:
        existing = _inspect_staging_writer_lock_path(lock_path)
        if existing is None:
            fd, opened = _open_or_create_staging_writer_lock_fd(
                lock_path, must_create=True
            )
            try:
                backend = _acquire_lock(fd, exclusive=True)
            except ByogPublicationLockError as error:
                raise StagingWriterLeaseError(
                    f"staging-writer lock backend is unsupported: {error}"
                ) from error
            except OSError as error:
                raise StagingWriterLeaseError(
                    f"cannot acquire staging writer lease {lock_path}: {error}"
                ) from error
            _validate_staging_writer_lock_fd(fd, lock_path, expected=opened)
        else:
            with _staging_writer_acquisition_gate(stage_dir) as gated:
                fd, opened = _open_existing_staging_writer_lock_fd(lock_path)
                try:
                    if gated:
                        backend = _try_acquire_exclusive_lock(fd)
                    else:
                        backend = _acquire_lock(fd, exclusive=True)
                except ByogPublicationLockError as error:
                    raise StagingWriterLeaseError(
                        f"staging-writer lock backend is unsupported: {error}"
                    ) from error
                except StagingWriterLeaseError:
                    raise
                except OSError as error:
                    raise StagingWriterLeaseError(
                        f"cannot acquire staging writer lease {lock_path}: {error}"
                    ) from error
                _validate_staging_writer_lock_fd(fd, lock_path, expected=opened)
        held = _HeldStagingWriterLease(
            fd, backend, lock_path, (opened.st_dev, opened.st_ino)
        )
        _after_staging_writer_lease(stage_dir)
        yield held
    finally:
        if held is not None:
            held.close()
        elif fd is not None and backend is not None:
            try:
                _release_lock(fd, backend)
            except OSError:
                pass
            os.close(fd)
        elif fd is not None:
            os.close(fd)


class HeldExistingStagingWriterClaim:
    """Exclusive claim on an existing ``.staging-writer.lock``.

    The lock file is never created, truncated, written, chmodded, or
    replaced. Identity is the observed regular-file (dev, ino, mode,
    mtime, size) tuple used by inventory revalidation. This is not
    ownership, writer death, or a graph-root lease.
    """

    def __init__(
        self,
        fd: int,
        backend: str,
        lock_path: Path,
        stage_dir: Path,
        identity: Tuple[int, int, int, int, int],
    ) -> None:
        self._fd = fd
        self._backend = backend
        self._lock_path = lock_path
        self._stage_dir = Path(stage_dir)
        self._identity = identity
        self._closed = False

    @property
    def fd(self) -> int:
        return self._fd

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    @property
    def stage_dir(self) -> Path:
        return self._stage_dir

    @property
    def identity(self) -> Tuple[int, int, int, int, int]:
        return self._identity

    @property
    def inode_identity(self) -> Tuple[int, int]:
        return (self._identity[0], self._identity[1])

    @property
    def closed(self) -> bool:
        return self._closed

    def release_and_remove(self) -> None:
        """Drop the kernel lease and unlink this claimed lock metadata."""
        self.close()
        try:
            current = self._lock_path.lstat()
        except FileNotFoundError as error:
            raise StagingWriterLockUnsafe(
                f"staging writer lock disappeared before removal: "
                f"{self._lock_path}"
            ) from error
        except OSError as error:
            raise StagingWriterLockUnsafe(
                f"cannot inspect staging writer lock before removal "
                f"{self._lock_path}: {error}"
            ) from error
        import stat as stat_mod

        if stat_mod.S_ISLNK(current.st_mode):
            _raise_staging_writer_lock_unsafe(
                self._lock_path,
                "unsafe symlinked staging writer lock is unsupported",
            )
        if not stat_mod.S_ISREG(current.st_mode) or (
            current.st_dev,
            current.st_ino,
        ) != self.inode_identity:
            _raise_staging_writer_lock_unsafe(
                self._lock_path,
                "staging writer lock changed before removal",
            )
        try:
            self._lock_path.unlink()
        except FileNotFoundError as error:
            raise StagingWriterLockUnsafe(
                f"staging writer lock disappeared before removal: "
                f"{self._lock_path}"
            ) from error
        except OSError as error:
            raise StagingWriterLeaseError(
                f"cannot remove staging writer lock {self._lock_path}: {error}"
            ) from error

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            _release_lock(self._fd, self._backend)
        except OSError:
            pass
        os.close(self._fd)


def _after_existing_staging_writer_claim(stage_dir: Path) -> None:
    """Test hook. Called after an existing writer lock is claimed."""
    return


def acquire_existing_staging_writer_claim(
    stage_dir: Path,
) -> HeldExistingStagingWriterClaim:
    """Nonblocking exclusive claim of an existing staging writer lock.

    Never creates a missing lock. Never truncates, writes, chmods, or
    replaces the file. Opens read-only, uses ``O_NOFOLLOW`` where
    available, and otherwise uses the existing read-only identity-checked
    fallback. Distinguishes contention from malformed/unsafe state.
    Does not infer PID, process, host, writer death, or ownership.
    """
    stage_dir = Path(stage_dir)
    if not is_staging_snapshot_name(stage_dir.name):
        raise StagingWriterLeaseError(
            f"existing writer-lock claim requires a private staging "
            f"directory: {stage_dir}"
        )
    lock_path = _staging_writer_lock_path(stage_dir)
    fd, opened = _open_existing_staging_writer_lock_fd(lock_path)
    backend: Optional[str] = None
    held: Optional[HeldExistingStagingWriterClaim] = None
    try:
        try:
            backend = _try_acquire_exclusive_lock(fd)
        except StagingWriterLockContention:
            raise
        except StagingWriterLeaseError:
            raise
        except OSError as error:
            raise StagingWriterLeaseError(
                f"cannot claim staging writer lease {lock_path}: {error}"
            ) from error
        opened = _validate_staging_writer_lock_fd(fd, lock_path, expected=opened)
        held = HeldExistingStagingWriterClaim(
            fd,
            backend,
            lock_path,
            stage_dir,
            (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_mtime_ns,
                opened.st_size,
            ),
        )
        _after_existing_staging_writer_claim(stage_dir)
        return held
    except Exception:
        if held is not None:
            held.close()
        else:
            if backend is not None:
                try:
                    _release_lock(fd, backend)
                except OSError:
                    pass
            os.close(fd)
        raise


@contextmanager
def claim_existing_staging_writer_lease(
    stage_dir: Path,
) -> Iterator[HeldExistingStagingWriterClaim]:
    """Context manager over :func:`acquire_existing_staging_writer_claim`."""
    held = acquire_existing_staging_writer_claim(stage_dir)
    try:
        yield held
    finally:
        held.close()


def probe_staging_writer_lease(stage_dir: Path) -> Dict[str, Any]:
    """Nonblocking observation of one staging directory's writer lease.

    Never creates, truncates, rewrites, chmods, or replaces the lock
    file. A contended probe means only that a cooperating process held
    the lease at that instant. A successful acquire-and-release means
    only that the lease was not held at that instant. Missing lock
    metadata is legacy/unverifiable. This is not a graph-root lease and
    does not infer ownership, writer death, or cleanup eligibility.
    """
    stage_dir = Path(stage_dir)
    lock_path = _staging_writer_lock_path(stage_dir)
    before = _inspect_staging_writer_lock_path(lock_path)
    if before is None:
        return {
            "writer_lease_protocol": "legacy_absent",
            "writer_lease_state": "unverifiable",
            "writer_lock_present": False,
            "writer_lock_regular": False,
            "identity": None,
        }
    fd, opened = _open_existing_staging_writer_lock_fd(lock_path)
    backend: Optional[str] = None
    try:
        try:
            backend = _try_acquire_exclusive_lock(fd)
        except StagingWriterLockContention:
            _validate_staging_writer_lock_fd(fd, lock_path, expected=opened)
            return {
                "writer_lease_protocol": "cooperative_v1",
                "writer_lease_state": "held_by_cooperating_writer",
                "writer_lock_present": True,
                "writer_lock_regular": True,
                "identity": (
                    opened.st_dev,
                    opened.st_ino,
                    opened.st_mode,
                    opened.st_mtime_ns,
                    opened.st_size,
                ),
            }
        except StagingWriterLeaseError:
            raise
        except OSError as error:
            raise StagingWriterLeaseError(
                f"cannot probe staging writer lease {lock_path}: {error}"
            ) from error
        _validate_staging_writer_lock_fd(fd, lock_path, expected=opened)
        return {
            "writer_lease_protocol": "cooperative_v1",
            "writer_lease_state": "not_held_at_scan",
            "writer_lock_present": True,
            "writer_lock_regular": True,
            "identity": (
                opened.st_dev,
                opened.st_ino,
                opened.st_mode,
                opened.st_mtime_ns,
                opened.st_size,
            ),
        }
    finally:
        if backend is not None:
            try:
                _release_lock(fd, backend)
            except OSError:
                pass
        os.close(fd)


def _write_snapshot_payload(
    snap_dir: Path,
    entities_df: pd.DataFrame,
    relationships_df: pd.DataFrame,
    text_units_df: pd.DataFrame,
    *,
    settings_text: str | None,
    source_root: Optional[Path],
    call_observations_df: Optional[pd.DataFrame],
    extra_manifest: Optional[Dict[str, Any]],
    snap_id: str,
    out_root: Path,
) -> None:
    _atomic_write_parquet(entities_df, snap_dir / "entities.parquet")
    _atomic_write_parquet(relationships_df, snap_dir / "relationships.parquet")
    _atomic_write_parquet(text_units_df, snap_dir / "text_units.parquet")

    has_obs = call_observations_df is not None and len(call_observations_df) > 0
    if has_obs:
        _atomic_write_parquet(call_observations_df, snap_dir / "call_observations.parquet")

    if settings_text:
        _atomic_write_text(settings_text, snap_dir / "settings.yaml")

    files_list = ["entities.parquet", "relationships.parquet", "text_units.parquet"]
    if has_obs:
        files_list.append("call_observations.parquet")
    manifest: Dict[str, Any] = {
        "id": snap_id,
        "created_at": datetime.now().isoformat(),
        "schema_version": 1,
        "counts": {
            "entities": len(entities_df),
            "relationships": len(relationships_df),
            "text_units": len(text_units_df),
            "call_observations": len(call_observations_df) if has_obs else 0,
        },
        "files": files_list,
        "source_root": str(source_root) if source_root else None,
        "git_commit": None,
        "total_size_bytes": None,
        "corpus_hash": None,
    }
    if extra_manifest:
        for k, v in extra_manifest.items():
            if k not in manifest:
                manifest[k] = v

    try:
        git_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=out_root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        manifest["git_commit"] = git_commit
    except Exception:
        pass

    total_size = 0
    size_files = ["entities.parquet", "relationships.parquet", "text_units.parquet"]
    if has_obs:
        size_files.append("call_observations.parquet")
    for fname in size_files:
        f = snap_dir / fname
        if f.exists():
            total_size += f.stat().st_size
    manifest["total_size_bytes"] = total_size

    _atomic_write_text(json.dumps(manifest, indent=2), snap_dir / "manifest.json")


def publish_byog_snapshot(
    entities_df: pd.DataFrame,
    relationships_df: pd.DataFrame,
    text_units_df: pd.DataFrame,
    out_root: Path,
    settings_text: str | None = None,
    keep_last: int = 5,
    source_root: Optional[Path] = None,
    call_observations_df: Optional[pd.DataFrame] = None,
    extra_manifest: Optional[Dict[str, Any]] = None,
) -> Path:
    """Write a complete BYOG snapshot and publish a 'current' pointer.

    Layout created:
        out_root/
            current                 # text file with the snapshot id
            .publish.lock           # exclusive publication/retention; shared readers
            snapshots/
                <id>/
                    entities.parquet
                    relationships.parquet
                    text_units.parquet
                    call_observations.parquet (if provided)
                    settings.yaml   (if provided)
                    manifest.json

    Payload files are written first in a private ``.staging-<id>`` directory
    under snapshots/. The publisher creates
    ``.staging-<id>/.staging-writer.lock`` immediately and holds an
    exclusive advisory writer lease for the complete staging-write
    interval, including the wait for the graph-root exclusive
    publication lock. Staging writes are not serialized by the graph
    lock. Staging names are not published ids and are never retention
    candidates. CLI indexers take ``.index.lock`` first and never hold
    this publication lock during extraction.

    Promotion of that directory, the ``current`` pointer update, and
    keep-last-N retention share one cross-process exclusive lock on
    ``.publish.lock``. While that graph-root lock is held the publisher
    releases and removes the staging writer-lock metadata, then
    atomically renames the staging directory. The published snapshot and
    ``manifest.files`` never contain the writer-lock file.

    ``current`` is only updated after the staging directory has been renamed
    into place, so it never names a staging directory or a partial snapshot.
    Cooperating readers take a shared lock on ``.publish.lock`` before
    resolving ``current`` and hold it until their files are materialized.
    Tools that ignore the lock are not protected.

    The persistent writer-lock file is protocol metadata, not proof of
    ownership or writer death. Process death releases the kernel lease
    and may leave the staging directory and lock file. That leftover is
    not orphaned, abandoned, expired, or safe to delete. The unavoidable
    directory-creation-to-lock-acquisition window is unverifiable.
    Retention does not reap staging dirs by guessed age.

    Cooperating keep-last cleanup protects ``current``, doc-claim pins, and
    operator pins from ``.snapshot-pins.json``. Selection uses the shared
    ``plan_snapshot_retention`` helper. The registry is validated under the
    publication lock before staging promotion or the ``current`` write. A
    malformed registry aborts publication before those mutations. An absent
    registry is a no-op and is not created.
    """
    out_root = Path(out_root)
    snapshots_dir = out_root / "snapshots"
    if snapshots_dir.is_symlink():
        raise ValueError(
            f"unsafe symlinked snapshots directory is unsupported: {snapshots_dir}"
        )
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    if snapshots_dir.is_symlink() or not snapshots_dir.is_dir():
        raise ValueError(f"snapshots path is not a real directory: {snapshots_dir}")

    snap_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
    if not is_published_snapshot_id(snap_id):
        raise RuntimeError(f"producer generated an unpublished snapshot id: {snap_id!r}")
    stage_dir = snapshots_dir / f"{STAGING_NAME_PREFIX}{snap_id}"
    final_dir = snapshots_dir / snap_id
    stage_dir.mkdir(parents=True, exist_ok=False)
    try:
        with staging_writer_lease(stage_dir) as writer_lease:
            _write_snapshot_payload(
                stage_dir,
                entities_df,
                relationships_df,
                text_units_df,
                settings_text=settings_text,
                source_root=source_root,
                call_observations_df=call_observations_df,
                extra_manifest=extra_manifest,
                snap_id=snap_id,
                out_root=out_root,
            )
            with _publication_lock(out_root):
                writer_lease.release_and_remove()
                leftover_lock = _staging_writer_lock_path(stage_dir)
                try:
                    leftover_lock.lstat()
                except FileNotFoundError:
                    pass
                else:
                    raise StagingWriterLeaseError(
                        "staging writer lock still present before promotion: "
                        f"{leftover_lock}"
                    )
                # Validate operator pins before promotion or current mutation.
                # An absent registry is an empty pin set and is not created.
                operator_pins = _operator_pins_for_retention(out_root)
                # The shared planner also validates current, claims, and every
                # snapshots/ entry. Do that before promotion so static invalid
                # retention state cannot turn a successful current update into a
                # reported publication failure.
                retention_before = _plan_snapshot_retention_locked(
                    out_root, keep_last=keep_last, operator_pins=operator_pins
                )
                try:
                    os.rename(stage_dir, final_dir)
                except OSError:
                    raise
                try:
                    _atomic_write_text(snap_id, out_root / "current")
                except Exception:
                    if final_dir.exists() and is_published_snapshot_id(final_dir.name):
                        current_file = out_root / "current"
                        current_id = ""
                        if current_file.is_file():
                            try:
                                current_id = current_file.read_text(encoding="utf-8").strip()
                            except OSError:
                                current_id = ""
                        if current_id != snap_id:
                            shutil.rmtree(final_dir)
                    raise
                try:
                    operator_after = _operator_pins_for_retention(out_root)
                except ValueError:
                    # Lock-ignoring registry mutation made pin state ambiguous.
                    # Skip deletion rather than retain or delete from a bad parse.
                    return final_dir
                if operator_after != operator_pins:
                    return final_dir
                try:
                    retention_after = _plan_snapshot_retention_locked(
                        out_root,
                        keep_last=keep_last,
                        operator_pins=operator_pins,
                    )
                except ValueError:
                    # A lock-ignoring actor changed a planner input after current
                    # was written. Publication is complete; skip deletion rather
                    # than report failure or act on ambiguous retention state.
                    return final_dir
                expected_published = _byte_sort_snapshot_ids(
                    [*retention_before["published_snapshots"], snap_id]
                )
                if (
                    retention_after["current"] != snap_id
                    or retention_after["published_snapshots"] != expected_published
                    or retention_after["claim_pins"] != retention_before["claim_pins"]
                ):
                    return final_dir
                _cleanup_old_snapshots_locked(
                    out_root,
                    keep_last=keep_last,
                    operator_pins=operator_pins,
                    retention_plan=retention_after,
                )
        return final_dir
    except Exception:
        _remove_staging_dir(stage_dir)
        raise


def _operator_pins_for_retention(out_root: Path) -> set[str]:
    """Validated operator pins. Caller must already hold the publication lock.

    An absent ``.snapshot-pins.json`` is an empty set and is not created.
    Malformed or unsafe registry state fails closed so publication and
    cleanup do not change ``current`` or delete snapshots from a bad parse.
    """
    from graphrag_code.snapshot_pins import (
        SnapshotPinsError,
        load_operator_pins_unlocked,
    )

    try:
        _revision, pins = load_operator_pins_unlocked(out_root)
    except SnapshotPinsError as error:
        raise ValueError(
            f"operator pin registry is unsafe or malformed: {error}"
        ) from error
    return set(pins)


def pinned_snapshot_ids(out_root: Path) -> set[str]:
    """Snapshot ids this graph must keep because a doc claim verifies them.

    `scripts/doc_claims.json` records `frozen_snapshot` claims against specific
    snapshot ids. Those are evidence, not cache: once retention deletes one the
    number it backed can never be re-derived, because the code that produced it
    has moved on. Operator pins live in ``.snapshot-pins.json`` and are loaded
    separately so a malformed registry cannot be confused with this set.
    """
    manifest = Path(__file__).resolve().parent / "doc_claims.json"
    if not manifest.is_file():
        return set()
    try:
        claims = json.loads(manifest.read_text()).get("claims") or []
    except (OSError, json.JSONDecodeError):
        return set()
    graph_name = Path(out_root).resolve().name
    pinned: set[str] = set()
    for claim in claims:
        source = claim.get("source") or {}
        snapshot = source.get("snapshot")
        if not isinstance(snapshot, str):
            continue
        if Path(str(source.get("graph") or "")).name == graph_name:
            pinned.add(snapshot)
    return pinned


def _byte_sort_snapshot_ids(values: Sequence[str]) -> List[str]:
    return sorted(dict.fromkeys(values), key=lambda item: item.encode("utf-8"))


def plan_snapshot_retention(
    *,
    keep_last: int,
    current_id: Optional[str],
    published_ids: Sequence[str],
    operator_pins: Sequence[str] = (),
    claim_pins: Sequence[str] = (),
) -> Dict[str, Any]:
    """Pure keep-last selection used by planning and cooperating cleanup.

    No I/O. Protected set is ``current`` UNION existing claim pins UNION
    existing operator pins. ``keep_last`` has an effective minimum of 1.
    Current is retained when it names a published directory. Every existing
    claim or operator pin is retained even when that set exceeds
    ``keep_last``. Newest remaining published ids fill the floor. Staging
    names are not published ids. Dangling pins are reported and never
    invented as retained snapshots. Published order is UTF-8-byte
    ascending, which is chronological for timestamped snapshot ids.
    """
    if isinstance(keep_last, bool) or not isinstance(keep_last, int):
        raise ValueError("keep_last must be an integer")
    keep_last_effective = max(1, keep_last)

    published: List[str] = []
    seen_published: set[str] = set()
    for item in published_ids:
        if not isinstance(item, str) or not is_published_snapshot_id(item):
            raise ValueError(
                f"published snapshot id is not a published id: {item!r}"
            )
        if item not in seen_published:
            published.append(item)
            seen_published.add(item)
    published.sort(key=lambda item: item.encode("utf-8"))
    published_set = set(published)

    def classify_pins(values: Sequence[str]) -> Tuple[List[str], List[str], List[str]]:
        all_pins: List[str] = []
        existing: List[str] = []
        dangling: List[str] = []
        seen: set[str] = set()
        for item in values:
            if not isinstance(item, str) or not is_published_snapshot_id(item):
                raise ValueError(f"pin id is not a published id: {item!r}")
            if item in seen:
                continue
            seen.add(item)
            all_pins.append(item)
            if is_published_snapshot_id(item) and item in published_set:
                existing.append(item)
            else:
                dangling.append(item)
        all_pins.sort(key=lambda item: item.encode("utf-8"))
        existing.sort(key=lambda item: item.encode("utf-8"))
        dangling.sort(key=lambda item: item.encode("utf-8"))
        return all_pins, existing, dangling

    operator, existing_operator, dangling_operator = classify_pins(operator_pins)
    claim, existing_claim, dangling_claim = classify_pins(claim_pins)
    effective = _byte_sort_snapshot_ids([*operator, *claim])

    current: Optional[str] = None
    keep: set[str] = set()
    if current_id is None or current_id == "":
        if published:
            raise ValueError("current snapshot id is required when snapshots are published")
    else:
        if not isinstance(current_id, str) or not is_published_snapshot_id(current_id):
            raise ValueError(
                f"current snapshot id is not a published id: {current_id!r}"
            )
        current = current_id
        if current not in published_set:
            raise ValueError(
                f"current snapshot is not a published snapshots/ directory: {current!r}"
            )
        keep.add(current)
    keep.update(existing_operator)
    keep.update(existing_claim)

    slots_left = max(0, keep_last_effective - len(keep))
    if slots_left > 0:
        newest_remaining = [sid for sid in published if sid not in keep]
        keep.update(newest_remaining[-slots_left:])

    retained = [sid for sid in published if sid in keep]
    deletion = [sid for sid in published if sid not in keep]
    return {
        "keep_last_effective": keep_last_effective,
        "current": current,
        "published_snapshots": published,
        "operator_pins": operator,
        "claim_pins": claim,
        "effective_pins": effective,
        "existing_operator_pins": existing_operator,
        "existing_claim_pins": existing_claim,
        "dangling_operator_pins": dangling_operator,
        "dangling_claim_pins": dangling_claim,
        "retained_snapshots": retained,
        "deletion_candidates": deletion,
    }


def _published_snapshot_ids_for_retention(snapshots_dir: Path) -> List[str]:
    """Published snapshot ids in canonical retention order. No I/O beyond listing."""
    import stat as stat_mod

    snapshots_dir = Path(snapshots_dir)
    if snapshots_dir.is_symlink():
        raise ValueError(
            f"unsafe symlinked snapshots directory is unsupported: {snapshots_dir}"
        )
    if not snapshots_dir.exists():
        return []
    if not snapshots_dir.is_dir():
        raise ValueError(f"snapshots path is not a real directory: {snapshots_dir}")
    try:
        entries = list(snapshots_dir.iterdir())
    except OSError as error:
        raise ValueError(f"cannot list {snapshots_dir}: {error}") from error
    published: List[str] = []
    for entry in entries:
        try:
            info = entry.lstat()
        except OSError as error:
            raise ValueError(f"cannot inspect {entry}: {error}") from error
        name = entry.name
        if stat_mod.S_ISLNK(info.st_mode):
            raise ValueError(f"unsafe symlinked snapshot entry: {entry}")
        if is_staging_snapshot_name(name):
            if not stat_mod.S_ISDIR(info.st_mode):
                raise ValueError(f"staging path is not a directory: {entry}")
            continue
        if is_published_snapshot_id(name) and stat_mod.S_ISDIR(info.st_mode):
            published.append(name)
            continue
        raise ValueError(
            f"unexpected unsafe snapshots entry is not published history: {entry}"
        )
    published.sort(key=lambda item: item.encode("utf-8"))
    return published


def _read_current_id_for_retention(out_root: Path) -> str:
    """Return the published current id. Fail closed on an unsafe pointer."""
    import stat as stat_mod

    current_file = Path(out_root) / "current"
    try:
        info = current_file.lstat()
    except FileNotFoundError as error:
        raise ValueError(f"unsafe or missing current pointer: {current_file}") from error
    except OSError as error:
        raise ValueError(f"cannot inspect current pointer {current_file}: {error}") from error
    if stat_mod.S_ISLNK(info.st_mode) or not stat_mod.S_ISREG(info.st_mode):
        raise ValueError(f"unsafe or missing current pointer: {current_file}")
    try:
        current_id = current_file.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise ValueError(f"cannot read current pointer {current_file}: {error}") from error
    if not is_published_snapshot_id(current_id):
        raise ValueError(
            f"current snapshot id is not a published id: {current_id!r}"
        )
    return current_id


def cleanup_old_snapshots(out_root: Path, keep_last: int = 5) -> int:
    """Delete old snapshot directories, keeping at most the most recent `keep_last`.

    - Always reads `current` first and protects that snapshot (never deletes it).
    - Also protects any snapshot a `frozen_snapshot` doc claim pins. Routine
      retention destroyed the sqlparse phase-5 baseline once: two reindexes
      published two new snapshots, the sixth-oldest fell off, and the claim that
      verified it degraded to an "absent source" skip that read as a pass.
    - Also protects operator pins from ``.snapshot-pins.json``. A malformed
      registry fails closed before any deletion. An absent registry is empty
      and is not created. Unpin does not run this cleanup.
    - Selection is ``plan_snapshot_retention``: the same helper used by
      ``snapshot-retention-plan``. Fail closed if that helper cannot produce
      a valid decision.
    - `keep_last` is clamped to at least 1 because current must be retained.
    - Keeps current plus the newest remaining snapshots up to the total limit.
    - Snapshot dirs are sorted by name (timestamped names sort chronologically).
    - Only published snapshot directories under snapshots/ are considered.
      Staging directories are ignored and are never deleted by retention.
    - Coordinates with publish_byog_snapshot() through the same exclusive lock.
    - Returns the number of deleted snapshot directories.
    """
    out_root = Path(out_root)
    snapshots_dir = out_root / "snapshots"
    if snapshots_dir.is_symlink():
        raise ValueError(
            f"unsafe symlinked snapshots directory is unsupported: {snapshots_dir}"
        )
    # Preserve the historical no-op contract: retention on a graph with no
    # snapshots must not create the graph root or a persistent lock artifact.
    if not snapshots_dir.exists():
        return 0
    with _publication_lock(out_root):
        operator_pins = _operator_pins_for_retention(out_root)
        return _cleanup_old_snapshots_locked(
            out_root, keep_last=keep_last, operator_pins=operator_pins
        )


def _cleanup_old_snapshots_locked(
    out_root: Path,
    keep_last: int = 5,
    *,
    operator_pins: Optional[set[str]] = None,
    retention_plan: Optional[Dict[str, Any]] = None,
) -> int:
    """Retention body. Caller must already hold ``_publication_lock``."""
    plan = retention_plan
    if plan is None:
        plan = _plan_snapshot_retention_locked(
            out_root,
            keep_last=keep_last,
            operator_pins=operator_pins,
        )
    else:
        try:
            validated_plan = plan_snapshot_retention(
                keep_last=keep_last,
                current_id=plan["current"],
                published_ids=plan["published_snapshots"],
                operator_pins=plan["operator_pins"],
                claim_pins=plan["claim_pins"],
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"invalid precomputed retention plan: {error}") from error
        if validated_plan != plan:
            raise ValueError("precomputed retention plan does not match its inputs")
        if operator_pins is not None and set(plan["operator_pins"]) != set(
            operator_pins
        ):
            raise ValueError("precomputed retention plan has different operator pins")
    out_root = Path(out_root)
    snapshots_dir = out_root / "snapshots"
    deleted = 0
    for snap_id in plan["deletion_candidates"]:
        target = snapshots_dir / snap_id
        try:
            shutil.rmtree(target)
            deleted += 1
        except Exception:
            # Best effort; do not fail the whole operation
            pass
    return deleted


def _plan_snapshot_retention_locked(
    out_root: Path,
    keep_last: int = 5,
    *,
    operator_pins: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Build the exact cooperating-cleanup plan without deleting anything.

    Caller must already hold ``_publication_lock``. This is used as a
    preflight before publication mutation and by cleanup immediately before
    deletion.
    """
    out_root = Path(out_root)
    snapshots_dir = out_root / "snapshots"
    if snapshots_dir.is_symlink():
        raise ValueError(
            f"unsafe symlinked snapshots directory is unsupported: {snapshots_dir}"
        )
    if not snapshots_dir.exists():
        return plan_snapshot_retention(
            keep_last=keep_last,
            current_id=None,
            published_ids=[],
            operator_pins=sorted(operator_pins or set()),
            claim_pins=sorted(pinned_snapshot_ids(out_root)),
        )
    if operator_pins is None:
        operator_pins = _operator_pins_for_retention(out_root)
    published_ids = _published_snapshot_ids_for_retention(snapshots_dir)
    current_path = out_root / "current"
    try:
        current_path.lstat()
        current_marker_exists = True
    except FileNotFoundError:
        current_marker_exists = False
    except OSError as error:
        raise ValueError(f"cannot inspect current pointer {current_path}: {error}") from error
    current_id = (
        _read_current_id_for_retention(out_root) if current_marker_exists else None
    )
    return plan_snapshot_retention(
        keep_last=keep_last,
        current_id=current_id,
        published_ids=published_ids,
        operator_pins=sorted(operator_pins),
        claim_pins=sorted(pinned_snapshot_ids(out_root)),
    )


class ByogGraph:
    """Lightweight in-memory view over a BYOG (entities, relationships, text_units)."""

    def __init__(self, graph_dir: Path):
        self.root = Path(graph_dir)
        with graph_read_lease(self.root, allow_unlocked_managed=True):
            self._load_tables()

    def _load_tables(self) -> None:
        self._load_tables_from_base(_resolve_graph_base(self.root))

    @classmethod
    def _from_resolved_base(cls, snap_dir: Path) -> "ByogGraph":
        """Load parquet from an already-resolved snapshot directory.

        Does not take a lease and does not consult ``current``. Callers must
        already hold any required graph-root lease and must have resolved
        ``snap_dir`` strictly beneath that graph.
        """
        obj = cls.__new__(cls)
        obj.root = Path(snap_dir)
        obj._load_tables_from_base(Path(snap_dir))
        return obj

    def _load_tables_from_base(self, base: Path) -> None:
        self._snap_base = Path(base)
        # These tables are fully materialized before the read lease is
        # released.  Synchronous decoding also avoids a Python 3.14/PyArrow
        # interpreter-shutdown deadlock where a short-lived CLI process exits
        # while Arrow worker callbacks are still trying to reacquire the GIL.
        parquet_options = {"use_threads": False}
        self.ents: pd.DataFrame = pd.read_parquet(
            self._snap_base / "entities.parquet", **parquet_options
        )
        self.rels: pd.DataFrame = pd.read_parquet(
            self._snap_base / "relationships.parquet", **parquet_options
        )
        tus_path = self._snap_base / "text_units.parquet"
        self.tus: pd.DataFrame = (
            pd.read_parquet(tus_path, **parquet_options)
            if tus_path.exists()
            else pd.DataFrame()
        )
        obs_path = self._snap_base / "call_observations.parquet"
        self.call_observations: pd.DataFrame = (
            pd.read_parquet(obs_path, **parquet_options)
            if obs_path.exists()
            else pd.DataFrame()
        )

        # Precompute for fast resolve
        self._title_to_row: Dict[str, pd.Series] = {
            str(row["title"]): row for _, row in self.ents.iterrows()
        }

    @property
    def titles(self) -> List[str]:
        return self.ents["title"].astype(str).tolist()

    def resolve(self, query: str) -> Optional[str]:
        """Return canonical title for exact/partial/module-alias query."""
        titles = self.ents["title"].astype(str)
        exact = self.ents[titles == query]
        if len(exact) == 1:
            return str(exact.iloc[0]["title"])

        # module alias support (e.g. "sim" -> "sim:sim")
        if "type" in self.ents.columns:
            types = self.ents["type"].astype(str).str.lower()
            module_alias = self.ents[
                (types == "module")
                & (
                    (titles == query)
                    | (titles == f"{query}:{query}")
                    | (titles == f"{query}:__module__")
                    | titles.str.endswith(":" + query)
                )
            ]
            if len(module_alias) == 1:
                return str(module_alias.iloc[0]["title"])

        partial = self.ents[titles.str.contains(query, case=False, na=False)]
        if len(partial) == 1:
            return str(partial.iloc[0]["title"])
        return None

    def get_entity(self, title: str) -> Optional[pd.Series]:
        t = self.resolve(title)
        if t and t in self._title_to_row:
            return self._title_to_row[t]
        return None

    def callers(self, symbol: str) -> List[str]:
        title = self.resolve(symbol)
        if not title:
            return []
        mask = (self.rels["target"].astype(str) == title) & (
            self.rels["type"].astype(str) == "calls"
        )
        return sorted(self.rels[mask]["source"].astype(str).unique().tolist())

    def callees(self, symbol: str) -> List[str]:
        title = self.resolve(symbol)
        if not title:
            return []
        mask = (self.rels["source"].astype(str) == title) & (
            self.rels["type"].astype(str) == "calls"
        )
        return sorted(self.rels[mask]["target"].astype(str).unique().tolist())

    def types_used_by(self, symbol: str) -> List[str]:
        """Sorted unique outgoing ``uses_type`` target titles.

        Recursive self-edges are preserved. Call-graph traversals
        (``callers``/``callees``/``impact``) are unaffected.
        """
        title = self.resolve(symbol)
        if not title:
            return []
        mask = (self.rels["source"].astype(str) == title) & (
            self.rels["type"].astype(str) == "uses_type"
        )
        return sorted(self.rels[mask]["target"].astype(str).unique().tolist())

    def type_users(self, symbol: str) -> List[str]:
        """Sorted unique incoming ``uses_type`` source titles.

        Recursive self-edges are preserved. Call-graph traversals are
        unaffected.
        """
        title = self.resolve(symbol)
        if not title:
            return []
        mask = (self.rels["target"].astype(str) == title) & (
            self.rels["type"].astype(str) == "uses_type"
        )
        return sorted(self.rels[mask]["source"].astype(str).unique().tolist())

    def type_closure(
        self,
        symbol: str,
        *,
        direction: str = "dependencies",
        max_depth: int = DEFAULT_TYPE_CLOSURE_MAX_DEPTH,
        max_nodes: int = DEFAULT_TYPE_CLOSURE_MAX_NODES,
        max_edges: int = DEFAULT_TYPE_CLOSURE_MAX_EDGES,
    ) -> Dict[str, Any]:
        """Bounded cycle-safe transitive ``uses_type`` closure (BFS, min depth).

        Only traverses relationships whose type is exactly ``uses_type``.
        Caps limit **returned** material; ``n_*_total`` counts remain exact
        within ``max_depth``. Self-edges are retained as evidence without
        duplicating the root node.
        """
        return compute_uses_type_closure(
            self.rels,
            self.resolve(symbol),
            direction=direction,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )

    def neighbors(self, symbol: str) -> Dict[str, List[str]]:
        title = self.resolve(symbol)
        if not title:
            return {"incoming": [], "outgoing": []}
        inc = self.rels[(self.rels["target"].astype(str) == title)]["source"].astype(str).unique().tolist()
        out = self.rels[(self.rels["source"].astype(str) == title)]["target"].astype(str).unique().tolist()
        return {"incoming": sorted(inc), "outgoing": sorted(out)}

    def impact(self, symbol: str) -> List[str]:
        """Transitive callers (affected symbols)."""
        title = self.resolve(symbol)
        if not title:
            return []
        from collections import defaultdict, deque

        rev: Dict[str, List[str]] = defaultdict(list)
        call_mask = self.rels["type"].astype(str) == "calls"
        for _, row in self.rels[call_mask].astype(str).iterrows():
            rev[row["target"]].append(row["source"])

        seen = set()
        q = deque([title])
        while q:
            cur = q.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            for pred in rev.get(cur, []):
                if pred not in seen:
                    q.append(pred)
        seen.discard(title)
        return sorted(seen)

    def dependency_order(self) -> List[str]:
        """Topological-ish order based on contains (modules/files first)."""
        contains = self.rels[self.rels["type"].astype(str) == "contains"][["source", "target"]].astype(str)
        from collections import defaultdict, deque

        graph: Dict[str, List[str]] = defaultdict(list)
        indeg: Dict[str, int] = defaultdict(int)
        all_nodes = set(self.ents["title"].astype(str))

        for _, row in contains.iterrows():
            src, tgt = row["source"], row["target"]
            graph[src].append(tgt)
            indeg[tgt] += 1
            all_nodes.add(src)
            all_nodes.add(tgt)

        q = deque([n for n in all_nodes if indeg.get(n, 0) == 0])
        order: List[str] = []
        while q:
            n = q.popleft()
            order.append(n)
            for nei in graph[n]:
                indeg[nei] -= 1
                if indeg[nei] == 0:
                    q.append(nei)
        remaining = sorted(all_nodes - set(order))
        return order + remaining

    def symbol(self, query: str) -> Optional[Dict[str, Any]]:
        title = self.resolve(query)
        if not title or title not in self._title_to_row:
            return None
        row = self._title_to_row[title]
        snippet = row.get("snippet") if "snippet" in row else None
        return {
            "title": title,
            "type": row.get("type"),
            "description": row.get("description"),
            "source_file": row.get("source_file"),
            "span": row.get("span"),
            "snippet_preview": str(snippet)[:200] if snippet else None,
        }

    def observations(self, query: str) -> List[Dict[str, Any]]:
        """Return weak/ambiguous/container call observations for a symbol or module.

        This is a lightweight diagnostic for the resolver (annotation tracking,
        reassignment guards, builtin containers, ambiguous unions) without
        materializing a full context pack.
        """
        if len(self.call_observations) == 0:
            return []
        title = self.resolve(query)
        if title:
            ent = self.get_entity(title)
            is_module = ent is not None and str(ent.get("type", "")).lower() == "module"
            if is_module:
                module_prefix = title.split(":", 1)[0]
                mask = (
                    (self.call_observations["source"].astype(str) == title) |
                    (self.call_observations["source"].astype(str) == module_prefix) |
                    self.call_observations["source"].astype(str).str.startswith(module_prefix + ":")
                )
            else:
                obs_src = self.call_observations["source"].astype(str)
                mask = (obs_src == title) | obs_src.str.startswith(title + ".")
        else:
            # treat raw query as prefix (e.g. "sim" or "sim:run_simulation")
            mask = self.call_observations["source"].astype(str).str.startswith(query)
        if not mask.any():
            return []
        cols = [c for c in ["source", "display_target", "confidence", "reason", "source_file", "span"]
                if c in self.call_observations.columns]
        return self.call_observations.loc[mask, cols].to_dict(orient="records")


# Back-compat helpers for existing code that expects dataframes
def load_byog(graph_dir: Path) -> Dict[str, pd.DataFrame]:
    g = ByogGraph(graph_dir)
    res = {
        "entities": g.ents,
        "relationships": g.rels,
        "text_units": g.tus,
    }
    if len(g.call_observations) > 0:
        res["call_observations"] = g.call_observations
    return res


def _empty_type_closure(
    *,
    root: Optional[str],
    direction: str,
    max_depth: int,
    max_nodes: int,
    max_edges: int,
    resolved: bool,
) -> Dict[str, Any]:
    return {
        "root": root,
        "resolved": resolved,
        "direction": direction,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "max_edges": max_edges,
        "nodes": [],
        "edges": [],
        "n_nodes_total": 0,
        "n_edges_total": 0,
        "n_nodes_returned": 0,
        "n_edges_returned": 0,
        "nodes_truncated": False,
        "edges_truncated": False,
    }


def compute_uses_type_closure(
    rels: pd.DataFrame,
    root_title: Optional[str],
    *,
    direction: str = "dependencies",
    max_depth: int = DEFAULT_TYPE_CLOSURE_MAX_DEPTH,
    max_nodes: int = DEFAULT_TYPE_CLOSURE_MAX_NODES,
    max_edges: int = DEFAULT_TYPE_CLOSURE_MAX_EDGES,
) -> Dict[str, Any]:
    """Pure BFS ``uses_type`` closure over a relationships table.

    Does not mutate inputs. Does not consult any other relationship type.
    ``max_nodes`` / ``max_edges`` only truncate the returned lists; totals
    within ``max_depth`` are always exact.
    """
    if direction not in TYPE_CLOSURE_DIRECTIONS:
        raise ValueError(
            f"unsupported type-closure direction {direction!r}; "
            f"expected one of {sorted(TYPE_CLOSURE_DIRECTIONS)}"
        )
    for name, value in (
        ("max_depth", max_depth),
        ("max_nodes", max_nodes),
        ("max_edges", max_edges),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer, got {value!r}")

    if not root_title:
        return _empty_type_closure(
            root=None,
            direction=direction,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            resolved=False,
        )

    # Adjacency over uses_type only. Neighbor lists sorted for deterministic BFS.
    out_adj: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    in_adj: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    edge_meta: Dict[str, Dict[str, str]] = {}

    if rels is not None and len(rels) > 0 and "type" in rels.columns:
        type_col = rels["type"].astype(str)
        uses = rels[type_col == "uses_type"]
        required = {"id", "source", "target"}
        missing_columns = sorted(required - set(uses.columns))
        if len(uses) and missing_columns:
            raise ValueError(
                "uses_type relationship table is missing required columns "
                f"{missing_columns!r}"
            )
        for row_index, row in uses.iterrows():
            values: Dict[str, str] = {}
            for field in sorted(required):
                raw = row.get(field)
                is_null = raw is None
                if not is_null:
                    try:
                        null_marker = pd.isna(raw)
                        is_null = bool(null_marker)
                    except (TypeError, ValueError):
                        is_null = False
                if is_null or not isinstance(raw, str) or not raw.strip():
                    raise ValueError(
                        f"uses_type relationship at row {row_index!r} has "
                        f"invalid {field}={raw!r}"
                    )
                values[field] = raw
            src = values["source"]
            tgt = values["target"]
            rid = values["id"]
            if rid in edge_meta:
                raise ValueError(
                    f"duplicate uses_type relationship id {rid!r}"
                )
            edge_meta[rid] = {"id": rid, "source": src, "target": tgt}
            out_adj[src].append((tgt, rid))
            in_adj[tgt].append((src, rid))

    for adj in (out_adj, in_adj):
        for key in list(adj.keys()):
            # Stable neighbor order: title then relationship id.
            adj[key] = sorted(set(adj[key]), key=lambda item: (item[0], item[1]))

    follow_out = direction in ("dependencies", "both")
    follow_in = direction in ("users", "both")

    depth_of: Dict[str, int] = {root_title: 0}
    # edge_id -> minimum expansion depth at which the edge was observed
    edge_depth: Dict[str, int] = {}
    queue: deque[str] = deque([root_title])

    while queue:
        cur = queue.popleft()
        cur_depth = depth_of[cur]
        if cur_depth >= max_depth:
            continue
        hops: List[Tuple[str, str]] = []
        if follow_out:
            hops.extend(out_adj.get(cur, []))
        if follow_in:
            hops.extend(in_adj.get(cur, []))
        # Deterministic expansion order when both directions apply.
        hops = sorted(set(hops), key=lambda item: (item[0], item[1]))
        for neighbor, rid in hops:
            prev_edge_depth = edge_depth.get(rid)
            if prev_edge_depth is None or cur_depth < prev_edge_depth:
                edge_depth[rid] = cur_depth
            # Self-edge: evidence only; root/node already present at min depth.
            if neighbor == cur:
                continue
            next_depth = cur_depth + 1
            if neighbor not in depth_of:
                depth_of[neighbor] = next_depth
                if next_depth < max_depth:
                    queue.append(neighbor)
                elif next_depth == max_depth:
                    # Node is in-range but must not expand further.
                    pass
            # BFS first visit is already min depth; ignore later longer paths.

    nodes_all = [
        {"title": title, "depth": depth}
        for title, depth in depth_of.items()
    ]
    nodes_all.sort(key=lambda n: (int(n["depth"]), str(n["title"])))

    edges_all: List[Dict[str, Any]] = []
    for rid, d in edge_depth.items():
        meta = edge_meta.get(rid)
        if meta is None:
            continue
        edges_all.append(
            {
                "id": meta["id"],
                "source": meta["source"],
                "target": meta["target"],
                "depth": int(d),
            }
        )
    edges_all.sort(
        key=lambda e: (
            int(e["depth"]),
            str(e["source"]),
            str(e["target"]),
            str(e["id"]),
        )
    )

    n_nodes_total = len(nodes_all)
    n_edges_total = len(edges_all)
    nodes_out = nodes_all[:max_nodes]
    edges_out = edges_all[:max_edges]

    return {
        "root": root_title,
        "resolved": True,
        "direction": direction,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "max_edges": max_edges,
        "nodes": nodes_out,
        "edges": edges_out,
        "n_nodes_total": n_nodes_total,
        "n_edges_total": n_edges_total,
        "n_nodes_returned": len(nodes_out),
        "n_edges_returned": len(edges_out),
        "nodes_truncated": n_nodes_total > len(nodes_out),
        "edges_truncated": n_edges_total > len(edges_out),
    }


def load_graph(graph_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    g = ByogGraph(graph_dir)
    return g.ents, g.rels
