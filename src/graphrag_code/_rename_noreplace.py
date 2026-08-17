"""Isolated native no-replace directory rename.

Publication of a completed export staging directory must not replace a
concurrently created destination, including an empty directory. Python's
``os.rename`` can replace an empty directory, so this module wraps the
platform libc primitives:

* Linux: ``renameat2(..., RENAME_NOREPLACE)``
* macOS: ``renameatx_np(..., RENAME_EXCL)``

Unsupported platforms fail closed. There is no ``os.rename`` fallback.
"""
from __future__ import annotations

import ctypes
import errno
import os
import sys
from typing import Optional

_RENAME_NOREPLACE_LINUX = 1
_RENAME_EXCL_DARWIN = 0x00000004


class RenameNoreplaceError(OSError):
    """Native no-replace rename failed."""


def _load_libc() -> Optional[ctypes.CDLL]:
    try:
        if sys.platform == "darwin":
            return ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        if sys.platform.startswith("linux"):
            return ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return None
    return None


def _bind_renameat2(libc: ctypes.CDLL) -> Optional[object]:
    try:
        func = libc.renameat2
    except AttributeError:
        return None
    func.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    func.restype = ctypes.c_int
    return func


def _bind_renameatx_np(libc: ctypes.CDLL) -> Optional[object]:
    try:
        func = libc.renameatx_np
    except AttributeError:
        return None
    func.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    func.restype = ctypes.c_int
    return func


_LIBC = _load_libc()
_RENAMEAT2 = _bind_renameat2(_LIBC) if _LIBC is not None else None
_RENAMEATX_NP = _bind_renameatx_np(_LIBC) if _LIBC is not None else None


def rename_noreplace_supported() -> bool:
    if sys.platform == "darwin":
        return _RENAMEATX_NP is not None
    if sys.platform.startswith("linux"):
        return _RENAMEAT2 is not None
    return False


def rename_directory_noreplace(
    old_dir_fd: int,
    old_name: str,
    new_dir_fd: int,
    new_name: str,
) -> None:
    """Atomically publish ``old_name`` to ``new_name`` without replacement.

    Both names are descriptor-relative direct names. If ``new_name`` already
    exists as any entry type, including an empty directory, the call fails
    and the destination is left untouched.
    """
    if not old_name or old_name in {".", ".."} or "/" in old_name or "\x00" in old_name:
        raise RenameNoreplaceError(errno.EINVAL, "old name is not a direct name")
    if not new_name or new_name in {".", ".."} or "/" in new_name or "\x00" in new_name:
        raise RenameNoreplaceError(errno.EINVAL, "new name is not a direct name")
    old_bytes = os.fsencode(old_name)
    new_bytes = os.fsencode(new_name)
    ctypes.set_errno(0)
    if sys.platform == "darwin" and _RENAMEATX_NP is not None:
        rc = _RENAMEATX_NP(
            int(old_dir_fd),
            old_bytes,
            int(new_dir_fd),
            new_bytes,
            _RENAME_EXCL_DARWIN,
        )
    elif sys.platform.startswith("linux") and _RENAMEAT2 is not None:
        rc = _RENAMEAT2(
            int(old_dir_fd),
            old_bytes,
            int(new_dir_fd),
            new_bytes,
            _RENAME_NOREPLACE_LINUX,
        )
    else:
        raise RenameNoreplaceError(
            errno.ENOTSUP,
            f"atomic no-replace directory rename is unsupported on {sys.platform!r}",
        )
    if rc == 0:
        return
    err = ctypes.get_errno() or errno.EIO
    raise RenameNoreplaceError(err, os.strerror(err))
