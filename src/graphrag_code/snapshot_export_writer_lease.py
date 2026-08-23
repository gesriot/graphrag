"""Cooperative writer-lease protocol for private export staging.

``snapshot-export-apply`` creates one persistent protocol file named
``.export-writer.lock`` immediately after anchoring a private
``.graphrag-export-<32 lowercase hex>`` staging directory. The file is
protocol metadata, not an export payload and not proof of ownership,
writer identity, writer death, crash, or cleanup eligibility.

This module is an internal helper. It is not a CLI command, not an MCP
tool, and not a graph lease. Apply uses exclusive creation plus a
blocking advisory lease. Inventory may inspect and nonblocking-probe
only that fixed lock name on recognized real staging directories.
Cleanup apply may take a fresh nonblocking exclusive claim on an
already-open existing lock descriptor. It never creates, replaces,
truncates, chmods, rewrites, or follows that file.

The pathname is removed while the advisory lease is still held, then
the lease is released before atomic publication. After that removal
the lease is not externally observable. A crash before lock creation may leave a staging directory
with missing metadata. A crash after lock creation may leave the
regular lock file; process death releases the kernel lease but does
not remove the file. A persistent lock file does not prove a writer
exists or existed. A successful nonblocking probe proves only that
the cooperative lease was not held at that instant. Contention proves
only that a cooperating process held the lease at that instant.
Neither state proves ownership, writer activity, writer death, age,
or safety to delete.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from graphrag_code.byog_graph import (
    ByogPublicationLockError,
    StagingWriterLeaseError,
    StagingWriterLockContention,
    _acquire_lock,
    _available_lock_backend,
    _release_lock,
    _try_acquire_exclusive_lock,
)

EXPORT_STAGING_WRITER_LOCK_NAME = ".export-writer.lock"
EXPORT_STAGING_WRITER_LOCK_MODE = 0o600

WRITER_LEASE_METADATA_ABSENT = "metadata_absent"
WRITER_LEASE_METADATA_UNSAFE = "metadata_unsafe"
WRITER_LEASE_HELD_AT_SCAN = "held_at_scan"
WRITER_LEASE_NOT_HELD_AT_SCAN = "not_held_at_scan"

_LOCK_IDENTITY_KEYS = (
    "writer_lease_dev",
    "writer_lease_ino",
    "writer_lease_mode",
    "writer_lease_size",
    "writer_lease_mtime_ns",
    "writer_lease_ctime_ns",
)


class ExportWriterLeaseError(Exception):
    """Unsupported platform or honest lease refusal. Exit 2."""

    exit_code = 2


class ExportWriterLeaseIntegrityError(ExportWriterLeaseError):
    """Pathname, identity, or metadata changed during lease use. Exit 1."""

    exit_code = 1


class ExportWriterLeaseUnsafe(ExportWriterLeaseIntegrityError):
    """Lock metadata is missing, replaced, symlinked, or not a regular file."""


def _after_export_writer_lock_path_inspected(staging_path: Path) -> None:
    """Test hook after no-follow lock lstat and before lock open."""
    return None


def _after_export_writer_lock_opened(staging_path: Path, lock_fd: int) -> None:
    """Test hook after the lock descriptor is accepted and before the probe."""
    return None


def _after_export_writer_lease_acquired(
    staging_path: Path, lock_fd: int
) -> None:
    """Test hook after apply holds the exclusive writer lease."""
    return None


def _after_export_writer_lock_removed_while_held(
    staging_path: Path, lock_fd: int
) -> None:
    """Test hook after lock-path removal and before lease release."""
    return None


def _after_existing_export_writer_claim(staging_path: Path, lock_fd: int) -> None:
    """Test hook after an existing leftover writer lock is claimed."""
    return None


def require_export_writer_lock_backend() -> str:
    backend = _available_lock_backend()
    if backend is None:
        raise ExportWriterLeaseError(
            "advisory export-writer lock is unsupported on "
            f"{sys.platform!r}; refusing to guess writer-lease state"
        )
    return backend


def require_export_writer_lock_probe_primitives() -> None:
    if (
        not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NOFOLLOW")
        or os.open not in getattr(os, "supports_dir_fd", set())
        or os.stat not in getattr(os, "supports_dir_fd", set())
        or os.scandir not in getattr(os, "supports_fd", set())
    ):
        raise ExportWriterLeaseError(
            "safe descriptor-relative no-follow export-writer lock "
            f"probes are unsupported on {sys.platform!r}"
        )
    require_export_writer_lock_backend()


def require_export_writer_lock_primitives() -> None:
    require_export_writer_lock_probe_primitives()
    if not hasattr(os, "O_EXCL") or os.unlink not in getattr(
        os, "supports_dir_fd", set()
    ):
        raise ExportWriterLeaseError(
            "safe descriptor-relative no-follow export-writer lock "
            f"creation is unsupported on {sys.platform!r}"
        )


def require_export_writer_lock_cleanup_primitives() -> None:
    """Probe plus descriptor-relative unlink/rmdir. Never creates a lock."""
    require_export_writer_lock_probe_primitives()
    supported = getattr(os, "supports_dir_fd", set())
    if os.unlink not in supported or os.rmdir not in supported:
        raise ExportWriterLeaseError(
            "safe descriptor-relative no-follow export-writer cleanup "
            f"is unsupported on {sys.platform!r}"
        )


def lock_file_identity(
    info: os.stat_result,
) -> Tuple[int, int, int, int, int, int]:
    """Complete no-follow lock token, including ctime."""
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def lock_inode_identity(info: os.stat_result) -> Tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _directory_inode(info: os.stat_result) -> Tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _open_flags(*extra: int) -> int:
    flags = 0
    for item in extra:
        flags |= item
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    return flags


def _stat_lock(staging_fd: int) -> Optional[os.stat_result]:
    try:
        return os.stat(
            EXPORT_STAGING_WRITER_LOCK_NAME,
            dir_fd=staging_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None


def _lock_is_safe_regular(info: os.stat_result) -> bool:
    return (
        not stat.S_ISLNK(info.st_mode)
        and stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
    )


def _nullable_lock_fields(
    info: Optional[os.stat_result],
) -> Dict[str, Optional[int]]:
    if info is None:
        return {key: None for key in _LOCK_IDENTITY_KEYS}
    return {
        "writer_lease_dev": info.st_dev,
        "writer_lease_ino": info.st_ino,
        "writer_lease_mode": info.st_mode,
        "writer_lease_size": info.st_size,
        "writer_lease_mtime_ns": info.st_mtime_ns,
        "writer_lease_ctime_ns": info.st_ctime_ns,
    }


def writer_lease_observation(
    *,
    state: str,
    staging_path: Path,
    info: Optional[os.stat_result],
    contended: bool,
) -> Dict[str, Any]:
    present = state != WRITER_LEASE_METADATA_ABSENT
    return {
        "writer_lease_state": state,
        "writer_lease_metadata_present": present,
        "writer_lease_contended": contended,
        "writer_lease_path": str(Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME),
        **_nullable_lock_fields(info),
    }


def _require_staging_held(
    parent_fd: int,
    staging_name: str,
    staging_fd: int,
    staging_identity: Tuple[int, int],
) -> os.stat_result:
    try:
        held = os.fstat(staging_fd)
        path_info = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise ExportWriterLeaseIntegrityError(
            f"private export staging directory changed while using the "
            f"writer lease: {staging_name}"
        ) from error
    if (
        not stat.S_ISDIR(held.st_mode)
        or stat.S_ISLNK(path_info.st_mode)
        or not stat.S_ISDIR(path_info.st_mode)
        or _directory_inode(held) != staging_identity
        or _directory_inode(path_info) != staging_identity
    ):
        raise ExportWriterLeaseIntegrityError(
            "private export staging directory no longer names the held "
            f"staging directory: {staging_name}"
        )
    return held


def _require_lock_fd_matches(
    lock_fd: int,
    staging_fd: int,
    expected: Tuple[int, int, int, int, int, int],
    *,
    staging_path: Path,
) -> os.stat_result:
    try:
        opened = os.fstat(lock_fd)
        current = os.stat(
            EXPORT_STAGING_WRITER_LOCK_NAME,
            dir_fd=staging_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise ExportWriterLeaseIntegrityError(
            "export writer lock disappeared while it was held: "
            f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
        ) from error
    except OSError as error:
        raise ExportWriterLeaseIntegrityError(
            "cannot re-inspect export writer lock "
            f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}: {error}"
        ) from error
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISREG(opened.st_mode)
        or not _lock_is_safe_regular(current)
        or lock_file_identity(opened) != expected
        or lock_file_identity(current) != expected
    ):
        raise ExportWriterLeaseIntegrityError(
            "export writer lock changed or became unsafe: "
            f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
        )
    return opened


def prove_export_writer_lock_absent(staging_fd: int, staging_path: Path) -> None:
    info = _stat_lock(staging_fd)
    if info is None:
        return
    raise ExportWriterLeaseIntegrityError(
        "export writer lock is still present after release: "
        f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
    )


def _acquire_backend(fd: int, *, blocking: bool) -> str:
    try:
        if blocking:
            return _acquire_lock(fd, exclusive=True)
        return _try_acquire_exclusive_lock(fd)
    except StagingWriterLockContention:
        raise
    except ByogPublicationLockError as error:
        raise ExportWriterLeaseError(
            f"export-writer lock backend is unsupported: {error}"
        ) from error
    except StagingWriterLeaseError as error:
        raise ExportWriterLeaseError(str(error)) from error
    except OSError as error:
        raise ExportWriterLeaseError(
            f"cannot acquire export writer lease: {error}"
        ) from error


class HeldExportWriterLease:
    """Exclusive lease on ``.export-writer.lock`` for one apply invocation."""

    def __init__(
        self,
        *,
        parent_fd: int,
        staging_name: str,
        staging_fd: int,
        staging_identity: Tuple[int, int],
        lock_fd: int,
        backend: str,
        lock_identity: Tuple[int, int, int, int, int, int],
        staging_path: Path,
    ) -> None:
        self._parent_fd = parent_fd
        self._staging_name = staging_name
        self._staging_fd = staging_fd
        self._staging_identity = staging_identity
        self._lock_fd = lock_fd
        self._backend = backend
        self._lock_identity = lock_identity
        self._staging_path = Path(staging_path)
        self._closed = False

    @property
    def fd(self) -> int:
        return self._lock_fd

    @property
    def staging_fd(self) -> int:
        return self._staging_fd

    @property
    def lock_identity(self) -> Tuple[int, int, int, int, int, int]:
        return self._lock_identity

    @property
    def inode_identity(self) -> Tuple[int, int]:
        return (self._lock_identity[0], self._lock_identity[1])

    @property
    def closed(self) -> bool:
        return self._closed

    def revalidate(self) -> os.stat_result:
        _require_staging_held(
            self._parent_fd,
            self._staging_name,
            self._staging_fd,
            self._staging_identity,
        )
        if self._closed:
            raise ExportWriterLeaseIntegrityError(
                "export writer lease was already released: "
                f"{self._staging_path / EXPORT_STAGING_WRITER_LOCK_NAME}"
            )
        return _require_lock_fd_matches(
            self._lock_fd,
            self._staging_fd,
            self._lock_identity,
            staging_path=self._staging_path,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            _release_lock(self._lock_fd, self._backend)
        except OSError:
            pass
        os.close(self._lock_fd)

    def release_and_remove(self) -> None:
        """Unlink this invocation's lock file, then drop the kernel lease.

        Unlinking while the lease is still held prevents a cooperating probe
        from acquiring the pathname's inode in an unlock/unlink window. The
        advisory lease is not externally observable after the pathname is
        removed. This is not proof that no other writer exists.
        """
        self.revalidate()
        try:
            try:
                current = os.stat(
                    EXPORT_STAGING_WRITER_LOCK_NAME,
                    dir_fd=self._staging_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError as error:
                raise ExportWriterLeaseIntegrityError(
                    "export writer lock disappeared before removal: "
                    f"{self._staging_path / EXPORT_STAGING_WRITER_LOCK_NAME}"
                ) from error
            except OSError as error:
                raise ExportWriterLeaseIntegrityError(
                    "cannot inspect export writer lock before removal "
                    f"{self._staging_path / EXPORT_STAGING_WRITER_LOCK_NAME}: {error}"
                ) from error
            if (
                stat.S_ISLNK(current.st_mode)
                or not _lock_is_safe_regular(current)
                or lock_file_identity(current) != self._lock_identity
            ):
                raise ExportWriterLeaseIntegrityError(
                    "export writer lock changed before removal: "
                    f"{self._staging_path / EXPORT_STAGING_WRITER_LOCK_NAME}"
                )
            try:
                os.unlink(EXPORT_STAGING_WRITER_LOCK_NAME, dir_fd=self._staging_fd)
            except FileNotFoundError as error:
                raise ExportWriterLeaseIntegrityError(
                    "export writer lock disappeared before removal: "
                    f"{self._staging_path / EXPORT_STAGING_WRITER_LOCK_NAME}"
                ) from error
            except OSError as error:
                raise ExportWriterLeaseError(
                    f"cannot remove export writer lock "
                    f"{self._staging_path / EXPORT_STAGING_WRITER_LOCK_NAME}: {error}"
                ) from error
            _after_export_writer_lock_removed_while_held(
                self._staging_path, self._lock_fd
            )
        finally:
            self.close()


def claim_existing_export_writer_lease(
    *,
    parent_fd: int,
    staging_name: str,
    staging_fd: int,
    staging_identity: Tuple[int, int],
    lock_fd: int,
    expected_lock_identity: Tuple[int, int, int, int, int, int],
    staging_path: Path,
) -> HeldExportWriterLease:
    """Nonblocking exclusive claim of an existing ``.export-writer.lock``.

    Never creates, truncates, writes, chmods, replaces, or follows the
    lock. Uses the caller's already-open no-follow descriptors. On
    success the returned lease owns ``lock_fd`` and will close it.
    Failure leaves ``lock_fd`` open for the caller. This is not
    ownership, writer death, or a graph lease.
    """
    require_export_writer_lock_cleanup_primitives()
    if not isinstance(lock_fd, int) or lock_fd < 0:
        raise ExportWriterLeaseIntegrityError(
            "export writer lock descriptor is missing for "
            f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
        )
    _require_staging_held(parent_fd, staging_name, staging_fd, staging_identity)
    opened = _require_lock_fd_matches(
        lock_fd,
        staging_fd,
        expected_lock_identity,
        staging_path=Path(staging_path),
    )
    if opened.st_size != 0:
        raise ExportWriterLeaseUnsafe(
            "export writer lock is not an empty regular file: "
            f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
        )
    if stat.S_IMODE(opened.st_mode) & 0o077:
        raise ExportWriterLeaseUnsafe(
            "export writer lock mode is permissive: "
            f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
        )
    backend: Optional[str] = None
    try:
        try:
            backend = _acquire_backend(lock_fd, blocking=False)
        except StagingWriterLockContention as error:
            raise ExportWriterLeaseIntegrityError(
                "export writer lease is held by a cooperating process: "
                f"{Path(staging_path)}"
            ) from error
        _require_staging_held(
            parent_fd, staging_name, staging_fd, staging_identity
        )
        _require_lock_fd_matches(
            lock_fd,
            staging_fd,
            expected_lock_identity,
            staging_path=Path(staging_path),
        )
        held = HeldExportWriterLease(
            parent_fd=parent_fd,
            staging_name=staging_name,
            staging_fd=staging_fd,
            staging_identity=staging_identity,
            lock_fd=lock_fd,
            backend=backend,
            lock_identity=expected_lock_identity,
            staging_path=Path(staging_path),
        )
        _after_existing_export_writer_claim(Path(staging_path), lock_fd)
        return held
    except Exception:
        if backend is not None:
            try:
                _release_lock(lock_fd, backend)
            except OSError:
                pass
        raise


def acquire_export_writer_lease(
    *,
    parent_fd: int,
    staging_name: str,
    staging_fd: int,
    staging_identity: Tuple[int, int],
    staging_path: Path,
) -> HeldExportWriterLease:
    """Create ``.export-writer.lock`` and take a blocking exclusive lease.

    The lock must be a newly created empty regular file. Existing,
    symlinked, or replaced pathnames are refused. This is not a graph
    lease and does not inspect payload contents.
    """
    require_export_writer_lock_primitives()
    _require_staging_held(parent_fd, staging_name, staging_fd, staging_identity)
    existing = _stat_lock(staging_fd)
    if existing is not None:
        raise ExportWriterLeaseUnsafe(
            "export writer lock already exists in a newly created staging "
            f"directory: {Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
        )
    _after_export_writer_lock_path_inspected(Path(staging_path))
    flags = _open_flags(
        os.O_RDWR, os.O_CREAT, os.O_EXCL, os.O_NOFOLLOW
    )
    try:
        lock_fd = os.open(
            EXPORT_STAGING_WRITER_LOCK_NAME,
            flags,
            EXPORT_STAGING_WRITER_LOCK_MODE,
            dir_fd=staging_fd,
        )
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.EEXIST, errno.ENOENT, errno.ENOTDIR}:
            raise ExportWriterLeaseUnsafe(
                "export writer lock changed or became unsafe while creating "
                f"it: {Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
            ) from error
        raise ExportWriterLeaseError(
            "cannot create export writer lock "
            f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}: {error}"
        ) from error
    backend: Optional[str] = None
    try:
        opened = os.fstat(lock_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_size != 0
            or opened.st_nlink != 1
        ):
            raise ExportWriterLeaseUnsafe(
                "export writer lock is not a newly created empty regular "
                f"file: {Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
            )
        path_info = _stat_lock(staging_fd)
        if (
            path_info is None
            or stat.S_ISLNK(path_info.st_mode)
            or not _lock_is_safe_regular(path_info)
            or lock_file_identity(path_info) != lock_file_identity(opened)
        ):
            raise ExportWriterLeaseUnsafe(
                "export writer lock changed while creating it: "
                f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
            )
        _after_export_writer_lock_opened(Path(staging_path), lock_fd)
        _require_staging_held(
            parent_fd, staging_name, staging_fd, staging_identity
        )
        opened = os.fstat(lock_fd)
        path_info = _stat_lock(staging_fd)
        if (
            path_info is None
            or lock_file_identity(opened) != lock_file_identity(path_info)
            or not _lock_is_safe_regular(path_info)
        ):
            raise ExportWriterLeaseIntegrityError(
                "export writer lock changed after it was opened: "
                f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
            )
        identity = lock_file_identity(opened)
        backend = _acquire_backend(lock_fd, blocking=True)
        _require_staging_held(
            parent_fd, staging_name, staging_fd, staging_identity
        )
        _require_lock_fd_matches(
            lock_fd, staging_fd, identity, staging_path=Path(staging_path)
        )
        held = HeldExportWriterLease(
            parent_fd=parent_fd,
            staging_name=staging_name,
            staging_fd=staging_fd,
            staging_identity=staging_identity,
            lock_fd=lock_fd,
            backend=backend,
            lock_identity=identity,
            staging_path=Path(staging_path),
        )
        _after_export_writer_lease_acquired(Path(staging_path), lock_fd)
        return held
    except Exception:
        if backend is not None:
            try:
                _release_lock(lock_fd, backend)
            except OSError:
                pass
        os.close(lock_fd)
        raise


def cleanup_owned_export_writer_lock(
    staging_fd: int,
    lock_identity: Tuple[int, int],
) -> None:
    """Unlink the lock only when the pathname still names this creation.

    Never follows a symlink and never deletes a replaced inode. Absence
    is not an error. This is not cleanup eligibility for leftovers
    found later by inventory.
    """
    try:
        info = os.stat(
            EXPORT_STAGING_WRITER_LOCK_NAME,
            dir_fd=staging_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return
    except OSError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return
    if lock_inode_identity(info) != lock_identity:
        return
    try:
        os.unlink(EXPORT_STAGING_WRITER_LOCK_NAME, dir_fd=staging_fd)
    except OSError:
        return


def _open_recognized_staging_fd(
    parent_fd: int,
    staging_name: str,
    expected: os.stat_result,
    staging_path: Path,
) -> int:
    expected_identity = (
        expected.st_dev,
        expected.st_ino,
        expected.st_size,
        expected.st_mtime_ns,
        expected.st_ctime_ns,
        expected.st_mode,
    )
    flags = _open_flags(os.O_RDONLY, os.O_DIRECTORY, os.O_NOFOLLOW)
    try:
        staging_fd = os.open(staging_name, flags, dir_fd=parent_fd)
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise ExportWriterLeaseIntegrityError(
                f"recognized export staging directory changed or became "
                f"unsafe while opening it: {staging_path}"
            ) from error
        raise ExportWriterLeaseError(
            f"cannot open recognized export staging directory {staging_path}: "
            f"{error}"
        ) from error
    try:
        opened = os.fstat(staging_fd)
        path_info = os.stat(staging_name, dir_fd=parent_fd, follow_symlinks=False)
        opened_identity = (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
            opened.st_mode,
        )
        path_identity = (
            path_info.st_dev,
            path_info.st_ino,
            path_info.st_size,
            path_info.st_mtime_ns,
            path_info.st_ctime_ns,
            path_info.st_mode,
        )
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(path_info.st_mode)
            or not stat.S_ISDIR(path_info.st_mode)
            or opened_identity != expected_identity
            or path_identity != expected_identity
        ):
            raise ExportWriterLeaseIntegrityError(
                "recognized export staging directory changed while opening "
                f"it: {staging_path}"
            )
    except Exception:
        os.close(staging_fd)
        raise
    return staging_fd


def _open_existing_export_writer_lock_fd(
    staging_fd: int,
    before: os.stat_result,
    staging_path: Path,
) -> int:
    flags = _open_flags(os.O_RDONLY, os.O_NOFOLLOW)
    try:
        lock_fd = os.open(
            EXPORT_STAGING_WRITER_LOCK_NAME, flags, dir_fd=staging_fd
        )
    except OSError as error:
        if error.errno in {errno.ELOOP, errno.ENOENT, errno.ENOTDIR}:
            raise ExportWriterLeaseIntegrityError(
                "export writer lock changed or became unsafe while opening "
                f"it: {Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
            ) from error
        raise ExportWriterLeaseError(
            "cannot open export writer lock "
            f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}: {error}"
        ) from error
    try:
        opened = os.fstat(lock_fd)
        current = _stat_lock(staging_fd)
        if (
            current is None
            or stat.S_ISLNK(current.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or not _lock_is_safe_regular(current)
            or lock_file_identity(opened) != lock_file_identity(before)
            or lock_file_identity(current) != lock_file_identity(before)
        ):
            raise ExportWriterLeaseIntegrityError(
                "export writer lock changed or became unsafe while opening "
                f"it: {Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
            )
    except Exception:
        os.close(lock_fd)
        raise
    return lock_fd


def probe_export_writer_lease(
    *,
    parent_fd: int,
    staging_name: str,
    staging_info: os.stat_result,
    staging_path: Path,
    keep_descriptors: bool = False,
) -> Tuple[Dict[str, Any], Optional[int], Optional[int]]:
    """Observe one recognized staging directory's writer-lease protocol file.

    Never creates, writes, truncates, chmods, replaces, unlinks, or
    renames lock metadata. Never follows a lock-file symlink. Never
    opens export payload contents. The exclusive probe, if acquired,
    is released before this function returns. ``keep_descriptors``
    leaves the staging and lock descriptors open for the caller; the
    kernel lease is still released.
    """
    require_export_writer_lock_probe_primitives()
    staging_fd = _open_recognized_staging_fd(
        parent_fd, staging_name, staging_info, Path(staging_path)
    )
    lock_fd: Optional[int] = None
    backend: Optional[str] = None
    retain = False
    try:
        before = _stat_lock(staging_fd)
        _after_export_writer_lock_path_inspected(Path(staging_path))
        after_hook = _stat_lock(staging_fd)
        if (before is None) != (after_hook is None):
            raise ExportWriterLeaseIntegrityError(
                "export writer lock presence changed while inspecting it: "
                f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
            )
        if before is None:
            observation = writer_lease_observation(
                state=WRITER_LEASE_METADATA_ABSENT,
                staging_path=Path(staging_path),
                info=None,
                contended=False,
            )
            retain = keep_descriptors
            return observation, (staging_fd if retain else None), None
        if after_hook is None or lock_file_identity(after_hook) != lock_file_identity(
            before
        ):
            raise ExportWriterLeaseIntegrityError(
                "export writer lock changed while inspecting it: "
                f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}"
            )
        if stat.S_ISLNK(before.st_mode) or not _lock_is_safe_regular(before):
            observation = writer_lease_observation(
                state=WRITER_LEASE_METADATA_UNSAFE,
                staging_path=Path(staging_path),
                info=before,
                contended=False,
            )
            retain = keep_descriptors
            return observation, (staging_fd if retain else None), None
        lock_fd = _open_existing_export_writer_lock_fd(
            staging_fd, before, Path(staging_path)
        )
        _after_export_writer_lock_opened(Path(staging_path), lock_fd)
        opened = _require_lock_fd_matches(
            lock_fd,
            staging_fd,
            lock_file_identity(before),
            staging_path=Path(staging_path),
        )
        try:
            backend = _acquire_backend(lock_fd, blocking=False)
        except StagingWriterLockContention:
            _require_lock_fd_matches(
                lock_fd,
                staging_fd,
                lock_file_identity(opened),
                staging_path=Path(staging_path),
            )
            observation = writer_lease_observation(
                state=WRITER_LEASE_HELD_AT_SCAN,
                staging_path=Path(staging_path),
                info=opened,
                contended=True,
            )
        else:
            _require_lock_fd_matches(
                lock_fd,
                staging_fd,
                lock_file_identity(opened),
                staging_path=Path(staging_path),
            )
            try:
                _release_lock(lock_fd, backend)
            except OSError as error:
                raise ExportWriterLeaseError(
                    "cannot release export-writer probe lease "
                    f"{Path(staging_path) / EXPORT_STAGING_WRITER_LOCK_NAME}: "
                    f"{error}"
                ) from error
            backend = None
            _require_lock_fd_matches(
                lock_fd,
                staging_fd,
                lock_file_identity(opened),
                staging_path=Path(staging_path),
            )
            observation = writer_lease_observation(
                state=WRITER_LEASE_NOT_HELD_AT_SCAN,
                staging_path=Path(staging_path),
                info=opened,
                contended=False,
            )
        retain = keep_descriptors
        retained_lock = lock_fd if retain else None
        if retain:
            lock_fd = None
        return observation, (staging_fd if retain else None), retained_lock
    finally:
        if backend is not None and lock_fd is not None:
            try:
                _release_lock(lock_fd, backend)
            except OSError:
                pass
        if lock_fd is not None:
            os.close(lock_fd)
        if not retain:
            os.close(staging_fd)



def close_held_probe_fds(held: Mapping[str, Tuple[Optional[int], Optional[int]]]) -> None:
    for staging_fd, lock_fd in held.values():
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except OSError:
                pass
        if staging_fd is not None:
            try:
                os.close(staging_fd)
            except OSError:
                pass
