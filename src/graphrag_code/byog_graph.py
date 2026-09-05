"""
Common BYOG graph loader and model (extracted from context_pack and graph_query).

Provides a clean ByogGraph class that both tools can use.

This reduces duplication and makes it easier to add local queries, module packs, etc.

All deterministic, no external API.
"""

from __future__ import annotations

import heapq
import json
import math
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

# Bounded multi-hop subgraph (relation-generic structural exploration).
# Caps match the existing MCP query hard limits (depth 32, 500/500).
SUBGRAPH_DIRECTIONS = frozenset({"outgoing", "incoming", "both"})
DEFAULT_SUBGRAPH_MAX_DEPTH = 3
DEFAULT_SUBGRAPH_MAX_NODES = 50
DEFAULT_SUBGRAPH_MAX_EDGES = 100
HARD_MAX_SUBGRAPH_DEPTH = 32
HARD_MAX_SUBGRAPH_NODES = 500
HARD_MAX_SUBGRAPH_EDGES = 500

# Weakly connected components (structural grouping summary only).
DEFAULT_COMPONENTS_MAX_COMPONENTS = 20
DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT = 20
HARD_MAX_COMPONENTS = 100
HARD_MAX_COMPONENT_NODES = 100

# Strongly connected components (directed mutual-reachability grouping only).
DEFAULT_STRONG_COMPONENTS_MAX_COMPONENTS = 20
DEFAULT_STRONG_COMPONENTS_MAX_NODES_PER_COMPONENT = 20
HARD_MAX_STRONG_COMPONENTS = 100
HARD_MAX_STRONG_COMPONENT_NODES = 100

# Directed SCC condensation DAG (structural presentation only).
DEFAULT_CONDENSATION_MAX_COMPONENTS = 20
DEFAULT_CONDENSATION_MAX_NODES_PER_COMPONENT = 20
DEFAULT_CONDENSATION_MAX_EDGES = 100
HARD_MAX_CONDENSATION_COMPONENTS = 100
HARD_MAX_CONDENSATION_COMPONENT_NODES = 100
HARD_MAX_CONDENSATION_EDGES = 500

# Directed structural shortest path (stored orientation only).
DEFAULT_SHORTEST_PATH_MAX_DEPTH = 8
HARD_MAX_SHORTEST_PATH_DEPTH = 32
_SHORTEST_PATH_STATUSES = frozenset(
    {
        "found",
        "unresolved_source",
        "unresolved_target",
        "unresolved_both",
        "not_found_within_max_depth",
    }
)

# Raw directed multigraph degree ranking (not centrality or importance).
DEGREE_RANKING_MODES = ("total", "incoming", "outgoing")
DEFAULT_DEGREE_RANKING_MAX_NODES = 20
HARD_MAX_DEGREE_RANKING_NODES = 100
_COMPONENTS_ENT_REQUIRED = ("title",)
SUBGRAPH_NODE_FIELDS = (
    "title",
    "depth",
    "id",
    "type",
    "description",
    "source_file",
    "span",
    "extractor",
    "confidence",
    "is_deterministic",
)
SUBGRAPH_EDGE_FIELDS = (
    "id",
    "source",
    "target",
    "type",
    "depth",
    "description",
    "weight",
    "source_file",
    "span",
    "extractor",
    "confidence",
    "is_deterministic",
    "fact_kind",
)
_SUBGRAPH_NODE_PROVENANCE = (
    "id",
    "type",
    "description",
    "source_file",
    "span",
    "extractor",
    "confidence",
    "is_deterministic",
)
_SUBGRAPH_EDGE_PROVENANCE = (
    "description",
    "weight",
    "source_file",
    "span",
    "extractor",
    "confidence",
    "is_deterministic",
    "fact_kind",
)
_SUBGRAPH_REL_REQUIRED = ("id", "source", "target", "type")

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

    def subgraph(
        self,
        symbol: str,
        *,
        direction: str = "both",
        max_depth: int = DEFAULT_SUBGRAPH_MAX_DEPTH,
        max_nodes: int = DEFAULT_SUBGRAPH_MAX_NODES,
        max_edges: int = DEFAULT_SUBGRAPH_MAX_EDGES,
        edge_types: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Bounded cycle-safe multi-hop induced subgraph (BFS, min depth).

        Relation-generic structural exploration over stored relationship
        rows. Caps limit **returned** material; ``n_*_total`` counts remain
        exact within ``max_depth`` and the type filter. Direction controls
        reachability only; returned ``source``/``target`` stay as stored.
        """
        return compute_bounded_subgraph(
            self.ents,
            self.rels,
            self.resolve(symbol),
            direction=direction,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            edge_types=edge_types,
        )

    def components(
        self,
        *,
        edge_types: Optional[Sequence[str]] = None,
        max_components: int = DEFAULT_COMPONENTS_MAX_COMPONENTS,
        max_nodes_per_component: int = DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT,
    ) -> Dict[str, Any]:
        """Weakly connected components over persisted structural relationships.

        Direction is ignored only for membership. Caps truncate returned
        material; totals stay exact for the selected snapshot and filter.
        This is a topology summary, not community detection.
        """
        return compute_weakly_connected_components(
            self.ents,
            self.rels,
            edge_types=edge_types,
            max_components=max_components,
            max_nodes_per_component=max_nodes_per_component,
        )

    def strong_components(
        self,
        *,
        edge_types: Optional[Sequence[str]] = None,
        max_components: int = DEFAULT_STRONG_COMPONENTS_MAX_COMPONENTS,
        max_nodes_per_component: int = DEFAULT_STRONG_COMPONENTS_MAX_NODES_PER_COMPONENT,
    ) -> Dict[str, Any]:
        """Directed strongly connected components over persisted relationships.

        Membership is exact mutual reachability on the selected directed
        topology. Caps truncate returned material; totals stay exact for
        the selected snapshot and filter. This is not weak connectivity,
        community detection, or a runtime-cycle proof.
        """
        return compute_strongly_connected_components(
            self.ents,
            self.rels,
            edge_types=edge_types,
            max_components=max_components,
            max_nodes_per_component=max_nodes_per_component,
        )

    def condensation(
        self,
        *,
        edge_types: Optional[Sequence[str]] = None,
        max_components: int = DEFAULT_CONDENSATION_MAX_COMPONENTS,
        max_nodes_per_component: int = DEFAULT_CONDENSATION_MAX_NODES_PER_COMPONENT,
        max_edges: int = DEFAULT_CONDENSATION_MAX_EDGES,
    ) -> Dict[str, Any]:
        """Directed SCC condensation DAG over persisted relationships.

        Components are exact mutual-reachability SCCs. Condensation edges
        are the distinct ordered SCC pairs induced by selected rows.
        Caps truncate returned material; totals stay exact. This is not
        weak connectivity, cycle enumeration, a unique topological rank,
        or a runtime-cycle proof.
        """
        return compute_condensation_graph(
            self.ents,
            self.rels,
            edge_types=edge_types,
            max_components=max_components,
            max_nodes_per_component=max_nodes_per_component,
            max_edges=max_edges,
        )

    def shortest_path(
        self,
        source: str,
        target: str,
        *,
        edge_types: Optional[Sequence[str]] = None,
        max_depth: int = DEFAULT_SHORTEST_PATH_MAX_DEPTH,
    ) -> Dict[str, Any]:
        """Directed structural shortest path over persisted relationships.

        Uses stored ``source -> target`` orientation only. Caps bound the
        search; a not-found result is not a global unreachability claim.
        This is not provenance, execution evidence, or semantic dependency.
        """
        return compute_shortest_path(
            self.ents,
            self.rels,
            self.resolve(source),
            self.resolve(target),
            edge_types=edge_types,
            max_depth=max_depth,
        )

    def degree_ranking(
        self,
        *,
        rank_by: str = "total",
        edge_types: Optional[Sequence[str]] = None,
        max_nodes: int = DEFAULT_DEGREE_RANKING_MAX_NODES,
    ) -> Dict[str, Any]:
        """Raw directed relationship-row degree ranking.

        Structural accounting only. Caps truncate returned rows; totals and
        degree sums stay exact. This is not PageRank, betweenness, closeness,
        eigenvector centrality, or semantic importance.
        """
        return compute_structural_degree_ranking(
            self.ents,
            self.rels,
            rank_by=rank_by,
            edge_types=edge_types,
            max_nodes=max_nodes,
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
        """Deterministic structural containment order over ``contains`` rows.

        Cross-component sources appear before their targets. UTF-8 order
        inside a cyclic component is presentation only. This is not a
        build, import, call, or semantic dependency order.
        """
        return compute_containment_dependency_order(self.ents, self.rels)

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


def _utf8_key(value: str) -> bytes:
    return value.encode("utf-8")


def _require_limit_int(name: str, value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(
            f"{name} must be an integer >= {minimum}, got {value!r}"
        )
    if value > maximum:
        raise ValueError(f"{name} must be <= {maximum}, got {value!r}")
    return value


def _require_nonempty_str(raw: Any, field: str, loc: Any) -> str:
    is_null = raw is None
    if not is_null:
        try:
            is_null = bool(pd.isna(raw))
        except (TypeError, ValueError):
            is_null = False
    if is_null or not isinstance(raw, str) or not raw:
        raise ValueError(f"relationship at row {loc!r} has invalid {field}={raw!r}")
    return raw


def _normalize_edge_types(
    edge_types: Optional[Sequence[str]],
) -> Optional[List[str]]:
    if edge_types is None:
        return None
    if isinstance(edge_types, (str, bytes)):
        raise ValueError(
            "edge_types must be a sequence of exact type strings, not a single string"
        )
    seen: set[str] = set()
    out: List[str] = []
    for item in edge_types:
        if not isinstance(item, str) or not item or item.strip() != item:
            raise ValueError(f"invalid edge-type filter {item!r}")
        if "\x00" in item:
            raise ValueError(f"invalid edge-type filter {item!r}")
        if item not in seen:
            seen.add(item)
            out.append(item)
    if not out:
        return None
    out.sort(key=_utf8_key)
    return out


def _subgraph_json_value(raw: Any) -> Any:
    """Normalize a stored scalar to a JSON-safe value.

    Pandas/Arrow nulls and NaN become JSON null. Inf is refused. Missing
    values are never stringified as ``"nan"``.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw
    if isinstance(raw, int) and not isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, float):
        if math.isnan(raw):
            return None
        if math.isinf(raw):
            raise ValueError(f"non-finite number is not JSON-safe: {raw!r}")
        return float(raw)
    try:
        if raw is getattr(pd, "NA", object()) or raw is getattr(pd, "NaT", object()):
            return None
    except Exception:
        pass
    try:
        if bool(pd.isna(raw)):
            return None
    except (TypeError, ValueError):
        pass
    item = getattr(raw, "item", None)
    if callable(item):
        try:
            converted = item()
        except (ValueError, AttributeError, TypeError):
            converted = None
        else:
            if converted is not raw:
                return _subgraph_json_value(converted)
    raise ValueError(f"unsupported subgraph field value {raw!r}")


def _empty_subgraph(
    *,
    root: Optional[str],
    direction: str,
    max_depth: int,
    max_nodes: int,
    max_edges: int,
    edge_types: Optional[List[str]],
    resolved: bool,
) -> Dict[str, Any]:
    return {
        "root": root,
        "resolved": resolved,
        "direction": direction,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "max_edges": max_edges,
        "edge_types": edge_types,
        "nodes": [],
        "edges": [],
        "n_nodes_total": 0,
        "n_edges_total": 0,
        "n_nodes_returned": 0,
        "n_edges_returned": 0,
        "nodes_truncated": False,
        "edges_truncated": False,
    }


def _entity_lookup(ents: Optional[pd.DataFrame]) -> Dict[str, Any]:
    by_title: Dict[str, Any] = {}
    if ents is None or len(ents) == 0 or "title" not in ents.columns:
        return by_title
    ranked: List[Tuple[bytes, bytes, Any]] = []
    for row_index, row in ents.iterrows():
        raw_title = row.get("title")
        try:
            if raw_title is None or bool(pd.isna(raw_title)):
                continue
        except (TypeError, ValueError):
            if raw_title is None:
                continue
        if not isinstance(raw_title, str) or not raw_title:
            continue
        raw_id = row.get("id") if "id" in row.index else ""
        id_text = raw_id if isinstance(raw_id, str) else ""
        ranked.append((_utf8_key(raw_title), _utf8_key(id_text), row))
    ranked.sort(key=lambda item: (item[0], item[1]))
    for title_key, _id_key, row in ranked:
        title = title_key.decode("utf-8")
        if title not in by_title:
            by_title[title] = row
    return by_title


def _node_record(title: str, depth: int, entity_row: Any) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "title": title,
        "depth": int(depth),
        "id": None,
        "type": None,
        "description": None,
        "source_file": None,
        "span": None,
        "extractor": None,
        "confidence": None,
        "is_deterministic": None,
    }
    if entity_row is None:
        return record
    for field in _SUBGRAPH_NODE_PROVENANCE:
        if field in entity_row.index:
            record[field] = _subgraph_json_value(entity_row.get(field))
    return record


def _edge_record(
    *,
    rid: str,
    source: str,
    target: str,
    rel_type: str,
    depth: int,
    row: Any,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "id": rid,
        "source": source,
        "target": target,
        "type": rel_type,
        "depth": int(depth),
        "description": None,
        "weight": None,
        "source_file": None,
        "span": None,
        "extractor": None,
        "confidence": None,
        "is_deterministic": None,
        "fact_kind": None,
    }
    for field in _SUBGRAPH_EDGE_PROVENANCE:
        if field in row.index:
            record[field] = _subgraph_json_value(row.get(field))
    return record


def compute_bounded_subgraph(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
    root_title: Optional[str],
    *,
    direction: str = "both",
    max_depth: int = DEFAULT_SUBGRAPH_MAX_DEPTH,
    max_nodes: int = DEFAULT_SUBGRAPH_MAX_NODES,
    max_edges: int = DEFAULT_SUBGRAPH_MAX_EDGES,
    edge_types: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Pure BFS induced subgraph over stored relationship rows.

    Direction controls which endpoints are followed during discovery.
    Returned edge ``source``/``target`` keep stored orientation. Caps
    truncate returned lists only; totals within ``max_depth`` and the
    type filter stay exact. Self-edges are evidence and do not duplicate
    the root or loop forever.
    """
    if not isinstance(direction, str) or direction not in SUBGRAPH_DIRECTIONS:
        raise ValueError(
            f"unsupported subgraph direction {direction!r}; "
            f"expected one of {sorted(SUBGRAPH_DIRECTIONS)}"
        )
    max_depth = _require_limit_int(
        "max_depth", max_depth, minimum=0, maximum=HARD_MAX_SUBGRAPH_DEPTH
    )
    max_nodes = _require_limit_int(
        "max_nodes",
        max_nodes,
        minimum=1,
        maximum=HARD_MAX_SUBGRAPH_NODES,
    )
    max_edges = _require_limit_int(
        "max_edges", max_edges, minimum=0, maximum=HARD_MAX_SUBGRAPH_EDGES
    )
    normalized_types = _normalize_edge_types(edge_types)

    if root_title is None:
        return _empty_subgraph(
            root=None,
            direction=direction,
            max_depth=max_depth,
            max_nodes=max_nodes,
            max_edges=max_edges,
            edge_types=normalized_types,
            resolved=False,
        )
    if not isinstance(root_title, str) or not root_title:
        raise ValueError(
            f"root_title must be a non-empty resolved title or null, got {root_title!r}"
        )

    allow = None if normalized_types is None else set(normalized_types)
    follow_out = direction in ("outgoing", "both")
    follow_in = direction in ("incoming", "both")

    out_adj: Dict[str, List[str]] = defaultdict(list)
    in_adj: Dict[str, List[str]] = defaultdict(list)
    filtered_rows: List[Tuple[str, str, str, str, Any]] = []
    seen_ids: set[str] = set()

    if rels is not None and len(rels) > 0:
        missing_columns = sorted(set(_SUBGRAPH_REL_REQUIRED) - set(rels.columns))
        if missing_columns:
            raise ValueError(
                "relationship table is missing required columns "
                f"{missing_columns!r}"
            )
        for row_index, row in rels.iterrows():
            values: Dict[str, str] = {}
            for field in _SUBGRAPH_REL_REQUIRED:
                values[field] = _require_nonempty_str(row.get(field), field, row_index)
            rel_type = values["type"]
            if allow is not None and rel_type not in allow:
                continue
            rid = values["id"]
            if rid in seen_ids:
                raise ValueError(f"duplicate relationship id {rid!r}")
            seen_ids.add(rid)
            src = values["source"]
            tgt = values["target"]
            filtered_rows.append((rid, src, tgt, rel_type, row))
            out_adj[src].append(tgt)
            in_adj[tgt].append(src)

    for adj in (out_adj, in_adj):
        for key in list(adj.keys()):
            adj[key] = sorted(set(adj[key]), key=_utf8_key)

    depth_of: Dict[str, int] = {root_title: 0}
    queue: deque[str] = deque([root_title])
    while queue:
        cur = queue.popleft()
        cur_depth = depth_of[cur]
        if cur_depth >= max_depth:
            continue
        hops: List[str] = []
        if follow_out:
            hops.extend(out_adj.get(cur, []))
        if follow_in:
            hops.extend(in_adj.get(cur, []))
        for neighbor in sorted(set(hops), key=_utf8_key):
            if neighbor == cur:
                continue
            if neighbor not in depth_of:
                next_depth = cur_depth + 1
                depth_of[neighbor] = next_depth
                if next_depth < max_depth:
                    queue.append(neighbor)

    entity_by_title = _entity_lookup(ents)
    reachable = set(depth_of)
    nodes_all = [
        _node_record(title, depth, entity_by_title.get(title))
        for title, depth in depth_of.items()
    ]
    root_nodes = [node for node in nodes_all if node["title"] == root_title]
    other_nodes = [node for node in nodes_all if node["title"] != root_title]
    other_nodes.sort(key=lambda node: (int(node["depth"]), _utf8_key(str(node["title"]))))
    nodes_all = root_nodes + other_nodes

    edges_all: List[Dict[str, Any]] = []
    for rid, src, tgt, rel_type, row in filtered_rows:
        if src not in reachable or tgt not in reachable:
            continue
        edge_depth = min(int(depth_of[src]), int(depth_of[tgt]))
        edges_all.append(
            _edge_record(
                rid=rid,
                source=src,
                target=tgt,
                rel_type=rel_type,
                depth=edge_depth,
                row=row,
            )
        )
    edges_all.sort(
        key=lambda edge: (
            int(edge["depth"]),
            _utf8_key(str(edge["source"])),
            _utf8_key(str(edge["target"])),
            _utf8_key(str(edge["type"])),
            _utf8_key(str(edge["id"])),
        )
    )

    n_nodes_total = len(nodes_all)
    n_edges_total = len(edges_all)
    nodes_out = nodes_all[:max_nodes]
    if not nodes_out or nodes_out[0]["title"] != root_title:
        raise ValueError(
            "max_nodes must be large enough to return the resolved root"
        )
    returned_titles = {str(node["title"]) for node in nodes_out}
    # Keep the returned material referentially closed: an edge is useful only
    # when both endpoint records are present. n_edges_total still describes
    # the complete induced relationship set before node/edge caps.
    returnable_edges = [
        edge
        for edge in edges_all
        if edge["source"] in returned_titles and edge["target"] in returned_titles
    ]
    edges_out = returnable_edges[:max_edges]

    return {
        "root": root_title,
        "resolved": True,
        "direction": direction,
        "max_depth": max_depth,
        "max_nodes": max_nodes,
        "max_edges": max_edges,
        "edge_types": normalized_types,
        "nodes": nodes_out,
        "edges": edges_out,
        "n_nodes_total": n_nodes_total,
        "n_edges_total": n_edges_total,
        "n_nodes_returned": len(nodes_out),
        "n_edges_returned": len(edges_out),
        "nodes_truncated": n_nodes_total > len(nodes_out),
        "edges_truncated": n_edges_total > len(edges_out),
    }


def compute_weakly_connected_components(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
    *,
    edge_types: Optional[Sequence[str]] = None,
    max_components: int = DEFAULT_COMPONENTS_MAX_COMPONENTS,
    max_nodes_per_component: int = DEFAULT_COMPONENTS_MAX_NODES_PER_COMPONENT,
) -> Dict[str, Any]:
    """Pure weakly-connected-components summary over stored relationship rows.

    Stored edge direction is ignored only when grouping nodes. Persisted
    rows are not rewritten. Caps truncate returned lists; every total is
    exact for the selected type filter. Isolated entity titles remain
    one-node components. Endpoint-only titles come only from selected
    relationship rows.
    """
    max_components = _require_limit_int(
        "max_components",
        max_components,
        minimum=1,
        maximum=HARD_MAX_COMPONENTS,
    )
    max_nodes_per_component = _require_limit_int(
        "max_nodes_per_component",
        max_nodes_per_component,
        minimum=1,
        maximum=HARD_MAX_COMPONENT_NODES,
    )
    normalized_types, entity_titles, selected, nodes = _topology_universe(
        ents, rels, edge_types=edge_types
    )

    parent = {title: title for title in nodes}

    def find(title: str) -> str:
        root = title
        while parent[root] != root:
            root = parent[root]
        while parent[title] != root:
            nxt = parent[title]
            parent[title] = root
            title = nxt
        return root

    def union(left: str, right: str) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left == root_right:
            return
        if _utf8_key(root_left) <= _utf8_key(root_right):
            parent[root_right] = root_left
        else:
            parent[root_left] = root_right

    for _rid, src, tgt in selected:
        union(src, tgt)

    members_by_root: Dict[str, List[str]] = defaultdict(list)
    for title in sorted(nodes, key=_utf8_key):
        members_by_root[find(title)].append(title)

    edge_counts: Dict[str, int] = defaultdict(int)
    for _rid, src, _tgt in selected:
        edge_counts[find(src)] += 1

    records: List[Dict[str, Any]] = []
    for root, members in members_by_root.items():
        titles = list(members)
        representative = titles[0]
        n_nodes = len(titles)
        n_entity = sum(1 for title in titles if title in entity_titles)
        returned_nodes = titles[:max_nodes_per_component]
        records.append(
            {
                "representative": representative,
                "nodes": returned_nodes,
                "n_nodes_total": n_nodes,
                "n_edges_total": int(edge_counts.get(root, 0)),
                "n_nodes_returned": len(returned_nodes),
                "n_entity_nodes": n_entity,
                "n_endpoint_only_nodes": n_nodes - n_entity,
                "nodes_truncated": n_nodes > len(returned_nodes),
            }
        )
    records.sort(
        key=lambda rec: (
            -int(rec["n_nodes_total"]),
            -int(rec["n_edges_total"]),
            _utf8_key(str(rec["representative"])),
        )
    )
    returned = records[:max_components]
    n_nodes_total = len(nodes)
    n_edges_total = len(selected)
    if sum(int(rec["n_nodes_total"]) for rec in records) != n_nodes_total:
        raise ValueError("component node totals do not cover the node universe")
    if sum(int(rec["n_edges_total"]) for rec in records) != n_edges_total:
        raise ValueError("component edge totals do not cover selected relationships")
    return {
        "edge_types": normalized_types,
        "max_components": max_components,
        "max_nodes_per_component": max_nodes_per_component,
        "components": returned,
        "n_components_total": len(records),
        "n_components_returned": len(returned),
        "n_nodes_total": n_nodes_total,
        "n_edges_total": n_edges_total,
        "n_entity_nodes_total": len(entity_titles),
        "n_endpoint_only_nodes_total": n_nodes_total - len(entity_titles),
        "components_truncated": len(records) > len(returned),
        "nodes_truncated": any(bool(rec["nodes_truncated"]) for rec in returned),
    }


def compute_structural_degree_ranking(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
    *,
    rank_by: str = "total",
    edge_types: Optional[Sequence[str]] = None,
    max_nodes: int = DEFAULT_DEGREE_RANKING_MAX_NODES,
) -> Dict[str, Any]:
    """Pure directed multigraph degree ranking over stored relationship rows.

    Each selected row adds one outgoing count at ``source`` and one incoming
    count at ``target``. Self-loops contribute 1/1/2. Parallel rows each
    count. Caps truncate returned rows; every total and degree sum stays
    exact. This is not centrality or importance.
    """
    if not isinstance(rank_by, str) or rank_by not in DEGREE_RANKING_MODES:
        raise ValueError(
            f"rank_by must be one of {list(DEGREE_RANKING_MODES)}, got {rank_by!r}"
        )
    max_nodes = _require_limit_int(
        "max_nodes",
        max_nodes,
        minimum=1,
        maximum=HARD_MAX_DEGREE_RANKING_NODES,
    )
    normalized_types, entity_titles, selected, nodes = _topology_universe(
        ents, rels, edge_types=edge_types
    )
    in_degree = {title: 0 for title in nodes}
    out_degree = {title: 0 for title in nodes}
    for _rid, src, tgt in selected:
        out_degree[src] += 1
        in_degree[tgt] += 1

    records: List[Dict[str, Any]] = []
    sum_in_degree = 0
    sum_out_degree = 0
    for title in nodes:
        incoming = int(in_degree[title])
        outgoing = int(out_degree[title])
        total = incoming + outgoing
        sum_in_degree += incoming
        sum_out_degree += outgoing
        records.append(
            {
                "title": title,
                "in_degree": incoming,
                "out_degree": outgoing,
                "total_degree": total,
                "is_entity": title in entity_titles,
            }
        )
    n_edges_total = len(selected)
    if sum_in_degree != n_edges_total:
        raise ValueError("in-degree sum does not equal selected relationship count")
    if sum_out_degree != n_edges_total:
        raise ValueError("out-degree sum does not equal selected relationship count")
    sum_total_degree = sum_in_degree + sum_out_degree
    if sum_total_degree != 2 * n_edges_total:
        raise ValueError(
            "total-degree sum does not equal twice the selected relationship count"
        )
    n_nodes_total = len(nodes)
    n_entity_nodes_total = len(entity_titles)
    n_endpoint_only_nodes_total = n_nodes_total - n_entity_nodes_total
    if n_entity_nodes_total + n_endpoint_only_nodes_total != n_nodes_total:
        raise ValueError("entity and endpoint-only counts do not cover the node universe")

    def sort_key(rec: Dict[str, Any]) -> Tuple[int, int, int, bytes]:
        title_key = _utf8_key(str(rec["title"]))
        incoming = -int(rec["in_degree"])
        outgoing = -int(rec["out_degree"])
        total = -int(rec["total_degree"])
        if rank_by == "incoming":
            return (incoming, total, outgoing, title_key)
        if rank_by == "outgoing":
            return (outgoing, total, incoming, title_key)
        return (total, incoming, outgoing, title_key)

    records.sort(key=sort_key)
    returned = records[:max_nodes]
    return {
        "rank_by": rank_by,
        "edge_types": normalized_types,
        "max_nodes": max_nodes,
        "nodes": returned,
        "n_nodes_total": n_nodes_total,
        "n_nodes_returned": len(returned),
        "n_edges_total": n_edges_total,
        "n_entity_nodes_total": n_entity_nodes_total,
        "n_endpoint_only_nodes_total": n_endpoint_only_nodes_total,
        "sum_in_degree": sum_in_degree,
        "sum_out_degree": sum_out_degree,
        "sum_total_degree": sum_total_degree,
        "nodes_truncated": n_nodes_total > len(returned),
    }


def compute_containment_dependency_order(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
) -> List[str]:
    """Deterministic containment order over persisted ``contains`` rows.

    Stored orientation is ``source contains target``. Every cross-SCC
    source appears before its target. Members of a directed cycle stay
    contiguous in UTF-8 title order. This is structural containment
    only, not a build, import, call, or semantic dependency order.
    """
    _normalized, _entity_titles, selected, nodes = _topology_universe(
        ents, rels, edge_types=["contains"]
    )
    if not nodes:
        return []
    pairs = {(src, tgt) for _rid, src, tgt in selected}
    return _containment_scc_order(nodes, pairs)


def compute_strongly_connected_components(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
    *,
    edge_types: Optional[Sequence[str]] = None,
    max_components: int = DEFAULT_STRONG_COMPONENTS_MAX_COMPONENTS,
    max_nodes_per_component: int = DEFAULT_STRONG_COMPONENTS_MAX_NODES_PER_COMPONENT,
) -> Dict[str, Any]:
    """Pure directed SCC summary over stored relationship rows.

    Membership is exact mutual reachability on the selected directed
    topology. Caps truncate returned lists; every total is exact for the
    selected type filter. Isolated entity titles remain singleton SCCs.
    Endpoint-only titles come only from selected relationship rows.
    """
    max_components = _require_limit_int(
        "max_components",
        max_components,
        minimum=1,
        maximum=HARD_MAX_STRONG_COMPONENTS,
    )
    max_nodes_per_component = _require_limit_int(
        "max_nodes_per_component",
        max_nodes_per_component,
        minimum=1,
        maximum=HARD_MAX_STRONG_COMPONENT_NODES,
    )
    normalized_types, entity_titles, selected, nodes = _topology_universe(
        ents, rels, edge_types=edge_types
    )
    n_nodes_total = len(nodes)
    n_edges_total = len(selected)
    n_entity_nodes_total = len(entity_titles)
    n_endpoint_only_nodes_total = n_nodes_total - n_entity_nodes_total
    if n_entity_nodes_total + n_endpoint_only_nodes_total != n_nodes_total:
        raise ValueError("entity and endpoint-only counts do not cover the node universe")

    empty = {
        "edge_types": normalized_types,
        "max_components": max_components,
        "max_nodes_per_component": max_nodes_per_component,
        "components": [],
        "n_components_total": 0,
        "n_components_returned": 0,
        "n_nodes_total": 0,
        "n_edges_total": 0,
        "n_internal_edges_total": 0,
        "n_cross_component_edges_total": 0,
        "n_self_loop_edges_total": 0,
        "n_cyclic_components_total": 0,
        "n_entity_nodes_total": 0,
        "n_endpoint_only_nodes_total": 0,
        "components_truncated": False,
        "nodes_truncated": False,
    }
    if not nodes:
        if n_edges_total != 0:
            raise ValueError("selected relationships exist without a node universe")
        return empty

    pairs = {(src, tgt) for _rid, src, tgt in selected}
    sccs = _iterative_sccs(nodes, pairs)
    title_to_scc = {
        title: index for index, members in enumerate(sccs) for title in members
    }
    if len(title_to_scc) != n_nodes_total or set(title_to_scc) != nodes:
        raise ValueError("SCC membership does not cover each node exactly once")

    n_sccs = len(sccs)
    internal_counts = [0] * n_sccs
    self_loop_counts = [0] * n_sccs
    n_cross = 0
    n_self_total = 0
    classified = 0
    for _rid, src, tgt in selected:
        src_scc = title_to_scc[src]
        tgt_scc = title_to_scc[tgt]
        classified += 1
        if src_scc == tgt_scc:
            internal_counts[src_scc] += 1
            if src == tgt:
                self_loop_counts[src_scc] += 1
                n_self_total += 1
        else:
            n_cross += 1
    if classified != n_edges_total:
        raise ValueError("selected relationship rows were not classified exactly once")

    records: List[Dict[str, Any]] = []
    n_cyclic = 0
    for index, members in enumerate(sccs):
        titles = list(members)
        n_nodes = len(titles)
        n_internal = int(internal_counts[index])
        n_self = int(self_loop_counts[index])
        n_entity = sum(1 for title in titles if title in entity_titles)
        is_cyclic = n_nodes > 1 or n_self > 0
        if is_cyclic:
            n_cyclic += 1
        if titles[0] != min(titles, key=_utf8_key):
            raise ValueError("SCC representative is not the minimum UTF-8 member")
        records.append(
            {
                "representative": titles[0],
                "nodes": titles,
                "n_nodes_total": n_nodes,
                "n_internal_edges_total": n_internal,
                "n_self_loop_edges_total": n_self,
                "n_entity_nodes": n_entity,
                "n_endpoint_only_nodes": n_nodes - n_entity,
                "is_cyclic": is_cyclic,
            }
        )
    records.sort(
        key=lambda rec: (
            -int(rec["n_nodes_total"]),
            -int(rec["n_internal_edges_total"]),
            _utf8_key(str(rec["representative"])),
        )
    )
    n_internal_total = sum(int(rec["n_internal_edges_total"]) for rec in records)
    n_self_sum = sum(int(rec["n_self_loop_edges_total"]) for rec in records)
    if sum(int(rec["n_nodes_total"]) for rec in records) != n_nodes_total:
        raise ValueError("component node totals do not cover the node universe")
    if n_internal_total + n_cross != n_edges_total:
        raise ValueError("internal and cross-component edges do not cover selected rows")
    if n_self_sum != n_self_total:
        raise ValueError("self-loop totals do not cover selected self-loop rows")

    returned: List[Dict[str, Any]] = []
    for rec in records[:max_components]:
        titles = list(rec["nodes"])
        shown = titles[:max_nodes_per_component]
        returned.append(
            {
                "representative": rec["representative"],
                "nodes": shown,
                "n_nodes_total": int(rec["n_nodes_total"]),
                "n_nodes_returned": len(shown),
                "n_internal_edges_total": int(rec["n_internal_edges_total"]),
                "n_self_loop_edges_total": int(rec["n_self_loop_edges_total"]),
                "n_entity_nodes": int(rec["n_entity_nodes"]),
                "n_endpoint_only_nodes": int(rec["n_endpoint_only_nodes"]),
                "is_cyclic": bool(rec["is_cyclic"]),
                "nodes_truncated": int(rec["n_nodes_total"]) > len(shown),
            }
        )
    return {
        "edge_types": normalized_types,
        "max_components": max_components,
        "max_nodes_per_component": max_nodes_per_component,
        "components": returned,
        "n_components_total": len(records),
        "n_components_returned": len(returned),
        "n_nodes_total": n_nodes_total,
        "n_edges_total": n_edges_total,
        "n_internal_edges_total": n_internal_total,
        "n_cross_component_edges_total": n_cross,
        "n_self_loop_edges_total": n_self_total,
        "n_cyclic_components_total": n_cyclic,
        "n_entity_nodes_total": n_entity_nodes_total,
        "n_endpoint_only_nodes_total": n_endpoint_only_nodes_total,
        "components_truncated": len(records) > len(returned),
        "nodes_truncated": any(bool(rec["nodes_truncated"]) for rec in returned),
    }


def compute_condensation_graph(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
    *,
    edge_types: Optional[Sequence[str]] = None,
    max_components: int = DEFAULT_CONDENSATION_MAX_COMPONENTS,
    max_nodes_per_component: int = DEFAULT_CONDENSATION_MAX_NODES_PER_COMPONENT,
    max_edges: int = DEFAULT_CONDENSATION_MAX_EDGES,
) -> Dict[str, Any]:
    """Directed SCC condensation DAG over stored relationship rows.

    Membership is exact mutual reachability on the selected directed
    topology. Condensation edges are the distinct ordered SCC pairs
    induced by selected rows; each stores the exact selected-row count
    for that pair. Caps truncate returned lists; every total is exact
    for the selected type filter. Isolated entity titles remain
    singleton SCCs. Endpoint-only titles come only from selected rows.
    """
    max_components = _require_limit_int(
        "max_components",
        max_components,
        minimum=1,
        maximum=HARD_MAX_CONDENSATION_COMPONENTS,
    )
    max_nodes_per_component = _require_limit_int(
        "max_nodes_per_component",
        max_nodes_per_component,
        minimum=1,
        maximum=HARD_MAX_CONDENSATION_COMPONENT_NODES,
    )
    max_edges = _require_limit_int(
        "max_edges",
        max_edges,
        minimum=0,
        maximum=HARD_MAX_CONDENSATION_EDGES,
    )
    normalized_types, entity_titles, selected, nodes = _topology_universe(
        ents, rels, edge_types=edge_types
    )
    n_nodes_total = len(nodes)
    n_edges_total = len(selected)
    n_entity_nodes_total = len(entity_titles)
    n_endpoint_only_nodes_total = n_nodes_total - n_entity_nodes_total
    if n_entity_nodes_total + n_endpoint_only_nodes_total != n_nodes_total:
        raise ValueError("entity and endpoint-only counts do not cover the node universe")

    empty = {
        "edge_types": normalized_types,
        "max_components": max_components,
        "max_nodes_per_component": max_nodes_per_component,
        "max_edges": max_edges,
        "components": [],
        "edges": [],
        "n_components_total": 0,
        "n_components_returned": 0,
        "n_nodes_total": 0,
        "n_edges_total": 0,
        "n_internal_edges_total": 0,
        "n_cross_component_edges_total": 0,
        "n_self_loop_edges_total": 0,
        "n_cyclic_components_total": 0,
        "n_entity_nodes_total": 0,
        "n_endpoint_only_nodes_total": 0,
        "n_condensation_edges_total": 0,
        "n_condensation_edges_eligible_total": 0,
        "n_condensation_edges_returned": 0,
        "components_truncated": False,
        "nodes_truncated": False,
        "edges_truncated": False,
    }
    if not nodes:
        if n_edges_total != 0:
            raise ValueError("selected relationships exist without a node universe")
        return empty

    pairs = {(src, tgt) for _rid, src, tgt in selected}
    sccs, order, title_to_scc, cond_pairs = _scc_condensation_dag(nodes, pairs)
    n_sccs = len(sccs)
    if n_sccs != len(order):
        raise ValueError("SCC condensation is not a DAG")
    if len(title_to_scc) != n_nodes_total or set(title_to_scc) != nodes:
        raise ValueError("SCC membership does not cover each node exactly once")

    internal_counts = [0] * n_sccs
    self_loop_counts = [0] * n_sccs
    cross_counts: Dict[Tuple[int, int], int] = {}
    n_cross = 0
    n_self_total = 0
    classified = 0
    for _rid, src, tgt in selected:
        src_scc = title_to_scc[src]
        tgt_scc = title_to_scc[tgt]
        classified += 1
        if src_scc == tgt_scc:
            internal_counts[src_scc] += 1
            if src == tgt:
                self_loop_counts[src_scc] += 1
                n_self_total += 1
        else:
            n_cross += 1
            key = (src_scc, tgt_scc)
            cross_counts[key] = cross_counts.get(key, 0) + 1
    if classified != n_edges_total:
        raise ValueError("selected relationship rows were not classified exactly once")
    if set(cross_counts) != cond_pairs:
        raise ValueError("condensation edges do not match cross-SCC topology")
    if sum(cross_counts.values()) != n_cross:
        raise ValueError("condensation-edge row counts do not cover cross-component rows")

    records: List[Dict[str, Any]] = []
    n_cyclic = 0
    covered_titles: List[str] = []
    for index in order:
        titles = list(sccs[index])
        n_nodes = len(titles)
        n_internal = int(internal_counts[index])
        n_self = int(self_loop_counts[index])
        n_entity = sum(1 for title in titles if title in entity_titles)
        is_cyclic = n_nodes > 1 or n_self > 0
        if is_cyclic:
            n_cyclic += 1
        if titles[0] != min(titles, key=_utf8_key):
            raise ValueError("SCC representative is not the minimum UTF-8 member")
        covered_titles.extend(titles)
        records.append(
            {
                "representative": titles[0],
                "nodes": titles,
                "n_nodes_total": n_nodes,
                "n_internal_edges_total": n_internal,
                "n_self_loop_edges_total": n_self,
                "n_entity_nodes": n_entity,
                "n_endpoint_only_nodes": n_nodes - n_entity,
                "is_cyclic": is_cyclic,
            }
        )
    if len(covered_titles) != n_nodes_total or set(covered_titles) != nodes:
        raise ValueError("component membership does not cover each node exactly once")
    n_internal_total = sum(int(rec["n_internal_edges_total"]) for rec in records)
    n_self_sum = sum(int(rec["n_self_loop_edges_total"]) for rec in records)
    if n_internal_total + n_cross != n_edges_total:
        raise ValueError("internal and cross-component edges do not cover selected rows")
    if n_self_sum != n_self_total:
        raise ValueError("self-loop totals do not cover selected self-loop rows")

    returned: List[Dict[str, Any]] = []
    for rec in records[:max_components]:
        titles = list(rec["nodes"])
        shown = titles[:max_nodes_per_component]
        returned.append(
            {
                "representative": rec["representative"],
                "nodes": shown,
                "n_nodes_total": int(rec["n_nodes_total"]),
                "n_nodes_returned": len(shown),
                "n_internal_edges_total": int(rec["n_internal_edges_total"]),
                "n_self_loop_edges_total": int(rec["n_self_loop_edges_total"]),
                "n_entity_nodes": int(rec["n_entity_nodes"]),
                "n_endpoint_only_nodes": int(rec["n_endpoint_only_nodes"]),
                "is_cyclic": bool(rec["is_cyclic"]),
                "nodes_truncated": int(rec["n_nodes_total"]) > len(shown),
            }
        )

    returned_indices = order[:max_components]
    returned_set = set(returned_indices)
    topo_pos = {index: pos for pos, index in enumerate(order)}
    eligible_rows: List[Tuple[int, int, int, int, int]] = []
    for src_scc, tgt_scc in cond_pairs:
        src_pos = topo_pos[src_scc]
        tgt_pos = topo_pos[tgt_scc]
        if src_pos >= tgt_pos:
            raise ValueError("condensation edge is not forward in topological order")
        if src_scc in returned_set and tgt_scc in returned_set:
            eligible_rows.append(
                (
                    src_pos,
                    tgt_pos,
                    src_scc,
                    tgt_scc,
                    int(cross_counts[(src_scc, tgt_scc)]),
                )
            )
    eligible_rows.sort()
    n_cond_total = len(cond_pairs)
    n_eligible = len(eligible_rows)
    shown_edges = eligible_rows[:max_edges]
    edges = [
        {
            "source": sccs[src_scc][0],
            "target": sccs[tgt_scc][0],
            "n_relationship_rows_total": count,
        }
        for _src_pos, _tgt_pos, src_scc, tgt_scc, count in shown_edges
    ]
    return {
        "edge_types": normalized_types,
        "max_components": max_components,
        "max_nodes_per_component": max_nodes_per_component,
        "max_edges": max_edges,
        "components": returned,
        "edges": edges,
        "n_components_total": len(records),
        "n_components_returned": len(returned),
        "n_nodes_total": n_nodes_total,
        "n_edges_total": n_edges_total,
        "n_internal_edges_total": n_internal_total,
        "n_cross_component_edges_total": n_cross,
        "n_self_loop_edges_total": n_self_total,
        "n_cyclic_components_total": n_cyclic,
        "n_entity_nodes_total": n_entity_nodes_total,
        "n_endpoint_only_nodes_total": n_endpoint_only_nodes_total,
        "n_condensation_edges_total": n_cond_total,
        "n_condensation_edges_eligible_total": n_eligible,
        "n_condensation_edges_returned": len(edges),
        "components_truncated": len(records) > len(returned),
        "nodes_truncated": any(bool(rec["nodes_truncated"]) for rec in returned),
        "edges_truncated": len(edges) < n_cond_total,
    }


def compute_shortest_path(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
    source_title: Optional[str],
    target_title: Optional[str],
    *,
    edge_types: Optional[Sequence[str]] = None,
    max_depth: int = DEFAULT_SHORTEST_PATH_MAX_DEPTH,
) -> Dict[str, Any]:
    """Directed structural shortest path over stored relationship rows.

    Topology uses distinct selected ordered pairs in stored orientation.
    Parallel rows count on the chosen steps only. Self-loops do not
    change traversal. Among minimum-hop paths, the complete node-title
    sequence that is smallest under UTF-8 byte order is returned.
    ``not_found_within_max_depth`` is not a global unreachability claim.
    """
    max_depth = _require_limit_int(
        "max_depth",
        max_depth,
        minimum=0,
        maximum=HARD_MAX_SHORTEST_PATH_DEPTH,
    )
    source_title = _optional_resolved_title(source_title, "source_title")
    target_title = _optional_resolved_title(target_title, "target_title")
    normalized_types, _entity_titles, selected, _nodes = _topology_universe(
        ents, rels, edge_types=edge_types
    )
    source_resolved = source_title is not None
    target_resolved = target_title is not None
    if not source_resolved or not target_resolved:
        if not source_resolved and not target_resolved:
            status = "unresolved_both"
        elif not source_resolved:
            status = "unresolved_source"
        else:
            status = "unresolved_target"
        return _shortest_path_payload(
            source=source_title,
            target=target_title,
            source_resolved=source_resolved,
            target_resolved=target_resolved,
            status=status,
            found=False,
            edge_types=normalized_types,
            max_depth=max_depth,
            distance=None,
            nodes=[],
            steps=[],
        )
    if source_title == target_title:
        return _shortest_path_payload(
            source=source_title,
            target=target_title,
            source_resolved=True,
            target_resolved=True,
            status="found",
            found=True,
            edge_types=normalized_types,
            max_depth=max_depth,
            distance=0,
            nodes=[source_title],
            steps=[],
        )

    pair_counts: Dict[Tuple[str, str], int] = {}
    neighbor_sets: Dict[str, set[str]] = defaultdict(set)
    for _rid, src, tgt in selected:
        pair_counts[(src, tgt)] = pair_counts.get((src, tgt), 0) + 1
        if src != tgt:
            neighbor_sets[src].add(tgt)
    adj: Dict[str, List[str]] = {
        src: sorted(dsts, key=_utf8_key) for src, dsts in neighbor_sets.items()
    }

    parent: Dict[str, Optional[str]] = {source_title: None}
    queue = deque([source_title])
    depth = {source_title: 0}
    reached = False
    while queue:
        node = queue.popleft()
        if depth[node] >= max_depth:
            continue
        for nxt in adj.get(node, ()):
            if nxt in parent:
                continue
            parent[nxt] = node
            depth[nxt] = depth[node] + 1
            if nxt == target_title:
                reached = True
                break
            queue.append(nxt)
        if reached:
            break
    if not reached:
        return _shortest_path_payload(
            source=source_title,
            target=target_title,
            source_resolved=True,
            target_resolved=True,
            status="not_found_within_max_depth",
            found=False,
            edge_types=normalized_types,
            max_depth=max_depth,
            distance=None,
            nodes=[],
            steps=[],
        )

    nodes = [target_title]
    while nodes[-1] != source_title:
        prev = parent[nodes[-1]]
        if prev is None:
            raise ValueError("shortest-path reconstruction lost the source")
        nodes.append(prev)
    nodes.reverse()
    steps: List[Dict[str, Any]] = []
    for src, tgt in zip(nodes, nodes[1:]):
        count = pair_counts.get((src, tgt))
        if count is None or count < 1:
            raise ValueError("shortest-path step is missing selected relationship rows")
        steps.append(
            {
                "source": src,
                "target": tgt,
                "n_relationship_rows_total": int(count),
            }
        )
    return _shortest_path_payload(
        source=source_title,
        target=target_title,
        source_resolved=True,
        target_resolved=True,
        status="found",
        found=True,
        edge_types=normalized_types,
        max_depth=max_depth,
        distance=len(steps),
        nodes=nodes,
        steps=steps,
    )


def _optional_resolved_title(raw: Any, name: str) -> Optional[str]:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"{name} must be a non-empty resolved title or null")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} is not strict UTF-8") from exc
    return raw


def _shortest_path_payload(
    *,
    source: Optional[str],
    target: Optional[str],
    source_resolved: bool,
    target_resolved: bool,
    status: str,
    found: bool,
    edge_types: Optional[List[str]],
    max_depth: int,
    distance: Optional[int],
    nodes: List[str],
    steps: List[Dict[str, Any]],
) -> Dict[str, Any]:
    if status not in _SHORTEST_PATH_STATUSES:
        raise ValueError(f"invalid shortest-path status {status!r}")
    if found != (status == "found"):
        raise ValueError("shortest-path found flag disagrees with status")
    if found:
        if source is None or target is None:
            raise ValueError("found shortest path is missing an endpoint")
        if distance is None or distance != len(steps):
            raise ValueError("found shortest-path distance must equal len(steps)")
        if len(nodes) != distance + 1:
            raise ValueError("found shortest path must have distance+1 nodes")
        if nodes[0] != source or nodes[-1] != target:
            raise ValueError("found shortest path must start at source and end at target")
        row_total = 0
        for index, step in enumerate(steps):
            if step["source"] != nodes[index] or step["target"] != nodes[index + 1]:
                raise ValueError("shortest-path step does not connect consecutive nodes")
            count = int(step["n_relationship_rows_total"])
            if count < 1:
                raise ValueError("shortest-path step row count must be >= 1")
            row_total += count
    else:
        if distance is not None or nodes or steps:
            raise ValueError("non-found shortest path must have empty material")
        row_total = 0
    return {
        "source": source,
        "target": target,
        "source_resolved": source_resolved,
        "target_resolved": target_resolved,
        "status": status,
        "found": found,
        "edge_types": edge_types,
        "max_depth": max_depth,
        "distance": distance,
        "nodes": nodes,
        "steps": steps,
        "n_nodes_returned": len(nodes),
        "n_steps_returned": len(steps),
        "n_relationship_rows_on_path_total": row_total,
    }


def _iterative_sccs(
    nodes: set[str],
    pairs: set[Tuple[str, str]],
) -> List[List[str]]:
    """Exact directed SCCs via iterative Kosaraju.

    Each component's members are sorted by UTF-8 title bytes. Discovery
    order is reverse-finish order, not ranking. Isolated nodes remain
    singleton components. Self-loops do not duplicate a node.
    """
    if not nodes:
        return []
    node_list = sorted(nodes, key=_utf8_key)
    adj: Dict[str, List[str]] = {title: [] for title in node_list}
    radj: Dict[str, List[str]] = {title: [] for title in node_list}
    for src, tgt in pairs:
        if src not in adj or tgt not in adj:
            raise ValueError("SCC edge endpoint is outside the node universe")
        adj[src].append(tgt)
        radj[tgt].append(src)
    for title in node_list:
        adj[title].sort(key=_utf8_key)
        radj[title].sort(key=_utf8_key)

    visited: set[str] = set()
    finish: List[str] = []
    for start in node_list:
        if start in visited:
            continue
        stack: List[Tuple[str, bool]] = [(start, False)]
        while stack:
            node, processed = stack.pop()
            if processed:
                finish.append(node)
                continue
            if node in visited:
                continue
            visited.add(node)
            stack.append((node, True))
            neighbors = adj[node]
            for nei in reversed(neighbors):
                if nei not in visited:
                    stack.append((nei, False))

    assigned: set[str] = set()
    sccs: List[List[str]] = []
    for start in reversed(finish):
        if start in assigned:
            continue
        members: List[str] = []
        stack_rev = [start]
        assigned.add(start)
        while stack_rev:
            node = stack_rev.pop()
            members.append(node)
            for nei in radj[node]:
                if nei not in assigned:
                    assigned.add(nei)
                    stack_rev.append(nei)
        members.sort(key=_utf8_key)
        sccs.append(members)

    covered = [title for members in sccs for title in members]
    if len(covered) != len(nodes) or set(covered) != nodes:
        raise ValueError("SCC membership does not cover each node exactly once")
    return sccs


def _scc_condensation_dag(
    nodes: set[str],
    pairs: set[Tuple[str, str]],
) -> Tuple[List[List[str]], List[int], Dict[str, int], set[Tuple[int, int]]]:
    """One SCC pass plus one deterministic condensation DAG.

    Shared by containment dependency-order and the condensation producer.
    Calls ``_iterative_sccs`` exactly once. Kahn order uses a heap keyed
    by each SCC representative's UTF-8 bytes; newly unlocked SCCs re-enter
    that same heap. Fails closed unless every SCC is emitted exactly once.
    """
    sccs = _iterative_sccs(nodes, pairs)
    title_to_scc = {
        title: index for index, members in enumerate(sccs) for title in members
    }
    cond_adj: Dict[int, set[int]] = {index: set() for index in range(len(sccs))}
    cond_indeg = [0] * len(sccs)
    cond_pairs: set[Tuple[int, int]] = set()
    for src, tgt in pairs:
        src_scc = title_to_scc[src]
        tgt_scc = title_to_scc[tgt]
        if src_scc == tgt_scc or tgt_scc in cond_adj[src_scc]:
            continue
        cond_adj[src_scc].add(tgt_scc)
        cond_indeg[tgt_scc] += 1
        cond_pairs.add((src_scc, tgt_scc))

    heap: List[Tuple[bytes, int]] = []
    for index, members in enumerate(sccs):
        if cond_indeg[index] == 0:
            heapq.heappush(heap, (_utf8_key(members[0]), index))
    ordered: List[int] = []
    while heap:
        _key, index = heapq.heappop(heap)
        ordered.append(index)
        for nxt in cond_adj[index]:
            cond_indeg[nxt] -= 1
            if cond_indeg[nxt] == 0:
                heapq.heappush(heap, (_utf8_key(sccs[nxt][0]), nxt))
    if len(ordered) != len(sccs) or len(set(ordered)) != len(sccs):
        raise ValueError("SCC condensation is not a DAG")
    return sccs, ordered, title_to_scc, cond_pairs


def _containment_scc_order(
    nodes: set[str],
    pairs: set[Tuple[str, str]],
) -> List[str]:
    sccs, ordered, _title_to_scc, _cond_pairs = _scc_condensation_dag(nodes, pairs)
    result: List[str] = []
    for index in ordered:
        result.extend(sccs[index])
    if len(result) != len(nodes) or len(set(result)) != len(result):
        raise ValueError("containment order does not cover each node exactly once")
    return result


def _topology_universe(
    ents: Optional[pd.DataFrame],
    rels: Optional[pd.DataFrame],
    *,
    edge_types: Optional[Sequence[str]],
) -> Tuple[Optional[List[str]], set[str], List[Tuple[str, str, str]], set[str]]:
    """Shared topology node universe for grouping, ranking, and SCC queries."""
    normalized_types = _normalize_edge_types(edge_types)
    entity_titles = _component_entity_titles(ents)
    selected = _component_selected_relationships(rels, normalized_types)
    nodes: set[str] = set(entity_titles)
    for _rid, src, tgt in selected:
        nodes.add(src)
        nodes.add(tgt)
    return normalized_types, entity_titles, selected, nodes


def _component_entity_titles(ents: Optional[pd.DataFrame]) -> set[str]:
    if ents is None:
        return set()
    if not isinstance(ents, pd.DataFrame):
        raise ValueError("entities must be a dataframe or null")
    if len(ents) == 0:
        return set()
    missing = sorted(set(_COMPONENTS_ENT_REQUIRED) - set(ents.columns))
    if missing:
        raise ValueError(f"entity table is missing required columns {missing!r}")
    titles: set[str] = set()
    for row_index, row in ents.iterrows():
        title = _require_component_str(row.get("title"), "title", row_index, kind="entity")
        if title in titles:
            raise ValueError(f"duplicate entity title {title!r}")
        titles.add(title)
    return titles


def _component_selected_relationships(
    rels: Optional[pd.DataFrame],
    allow: Optional[List[str]],
) -> List[Tuple[str, str, str]]:
    if rels is None:
        return []
    if not isinstance(rels, pd.DataFrame):
        raise ValueError("relationships must be a dataframe or null")
    if len(rels) == 0:
        return []
    missing = sorted(set(_SUBGRAPH_REL_REQUIRED) - set(rels.columns))
    if missing:
        raise ValueError(
            "relationship table is missing required columns " f"{missing!r}"
        )
    allow_set = None if allow is None else set(allow)
    seen_ids: set[str] = set()
    selected: List[Tuple[str, str, str]] = []
    for row_index, row in rels.iterrows():
        values: Dict[str, str] = {}
        for field in _SUBGRAPH_REL_REQUIRED:
            values[field] = _require_component_str(
                row.get(field), field, row_index, kind="relationship"
            )
        rid = values["id"]
        if rid in seen_ids:
            raise ValueError(f"duplicate relationship id {rid!r}")
        seen_ids.add(rid)
        if allow_set is not None and values["type"] not in allow_set:
            continue
        selected.append((rid, values["source"], values["target"]))
    return selected


def _require_component_str(raw: Any, field: str, loc: Any, *, kind: str) -> str:
    is_null = raw is None
    if not is_null:
        try:
            is_null = bool(pd.isna(raw))
        except (TypeError, ValueError):
            is_null = False
    if is_null or not isinstance(raw, str) or not raw:
        raise ValueError(f"{kind} at row {loc!r} has invalid {field}={raw!r}")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{kind} at row {loc!r} has invalid {field}={raw!r}"
        ) from exc
    return raw


def load_graph(graph_dir: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    g = ByogGraph(graph_dir)
    return g.ents, g.rels
