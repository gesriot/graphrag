"""Content-addressed whole-snapshot reuse for CLI indexing.

This is opt-in snapshot reuse, not per-file incremental graph patching and
not a watcher. Supported deterministic configurations may record an
``index_input`` fingerprint and skip extraction when that fingerprint, the
producer digest, and ``source_root`` still match a doctor-valid current
snapshot.

Lock ordering
-------------
1. ``.index.lock`` (this module) covers one CLI index of a graph root:
   reuse lookup, extraction, the mid-index stability re-hash, and the call
   into ``publish_byog_snapshot``.
2. ``.publish.lock`` is acquired only inside ``publish_byog_snapshot`` for
   the short rename / ``current`` / retention window. It is never held
   during extraction.
3. Never acquire ``.index.lock`` while already holding ``.publish.lock``.

A wheel install hashes packaged ``graphrag_code`` sources by relative path
and file bytes. Installation prefixes and mtimes are not part of the key.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version as _distribution_version
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from graphrag_code.c_identities import list_indexed_c_files, package_relative_posix
from graphrag_code.paths import PACKAGE_DIR
from graphrag_code.python_inputs import list_indexed_python_files

INDEX_LOCK_NAME = ".index.lock"
INDEX_INPUT_SCHEMA_VERSION = 1
INDEX_INPUT_ALGORITHM = "sha256"
INDEX_INPUT_PROTOCOL = "graphrag-code.index_input.v1"
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_PRODUCER_RESOURCE_NAMES = frozenset({"doc_claims.json"})
_PRODUCER_DISTRIBUTIONS = (
    "duckdb",
    "graphrag",
    "networkx",
    "pandas",
    "pyarrow",
    "python-dotenv",
    "tree-sitter",
    "tree-sitter-c",
    "tree-sitter-python",
    "typer",
)

PYTHON_OPTION_KEYS: Tuple[str, ...] = ("use_advanced",)
C_OPTION_KEYS: Tuple[str, ...] = (
    "compiler_builtins",
    "compiler_dependencies",
    "compiler_includes",
    "clang_signatures",
    "clang_calls",
    "clang_types",
    "clang_type_uses",
    "clang_type_shapes",
)
SUPPORTED_BLOCK_KEYS: Tuple[str, ...] = (
    "schema_version",
    "algorithm",
    "indexer",
    "options",
    "n_files",
    "producer_digest",
    "digest",
    "reuse_supported",
)
UNSUPPORTED_BLOCK_KEYS: Tuple[str, ...] = SUPPORTED_BLOCK_KEYS + ("reason",)


class IndexBuildLockError(RuntimeError):
    """Raised when the per-graph CLI index lock cannot be taken honestly."""


def canonical_python_options(*, use_advanced: bool) -> Dict[str, bool]:
    return {"use_advanced": bool(use_advanced)}


def canonical_c_options(
    *,
    compiler_builtins: bool = False,
    compiler_dependencies: bool = False,
    compiler_includes: bool = False,
    clang_signatures: bool = False,
    clang_calls: bool = False,
    clang_types: bool = False,
    clang_type_uses: bool = False,
    clang_type_shapes: bool = False,
) -> Dict[str, bool]:
    return {
        "compiler_builtins": bool(compiler_builtins),
        "compiler_dependencies": bool(compiler_dependencies),
        "compiler_includes": bool(compiler_includes),
        "clang_signatures": bool(clang_signatures),
        "clang_calls": bool(clang_calls),
        "clang_types": bool(clang_types),
        "clang_type_uses": bool(clang_type_uses),
        "clang_type_shapes": bool(clang_type_shapes),
    }


def unsupported_reuse_options(indexer: str, options: Mapping[str, Any]) -> List[str]:
    """Return enabled option names that have no complete reuse cache key."""
    if indexer == "python":
        return [key for key in PYTHON_OPTION_KEYS if bool(options.get(key))]
    if indexer == "c":
        return [key for key in C_OPTION_KEYS if bool(options.get(key))]
    return [f"indexer:{indexer}"]


def reuse_supported(indexer: str, options: Mapping[str, Any]) -> bool:
    return indexer in {"python", "c"} and not unsupported_reuse_options(indexer, options)


def reject_unsupported_reuse(indexer: str, options: Mapping[str, Any]) -> None:
    """Exit 2 before extraction when explicit reuse is asked for an incomplete key."""
    bad = unsupported_reuse_options(indexer, options)
    if not bad:
        return
    named = ", ".join(bad)
    print(
        f"graphrag-code: --reuse-unchanged does not support {named}. "
        "Those modes are toolchain-dependent and do not have a complete "
        "cache key yet. Omit --reuse-unchanged to rebuild.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _length_prefixed(payload: bytes) -> bytes:
    return len(payload).to_bytes(8, "big") + payload


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_options_bytes(options: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(options),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def producer_digest() -> str:
    """Content/runtime digest of the installed producer. Path-independent."""
    root = PACKAGE_DIR.resolve()
    hasher = hashlib.sha256()
    hasher.update(_length_prefixed(INDEX_INPUT_PROTOCOL.encode("utf-8")))
    hasher.update(_length_prefixed(b"producer"))
    runtime_entries = [
        ("python-implementation", sys.implementation.name),
        ("python-version", ".".join(str(part) for part in sys.version_info[:3])),
        ("python-cache-tag", str(getattr(sys.implementation, "cache_tag", None))),
    ]
    for distribution in _PRODUCER_DISTRIBUTIONS:
        try:
            installed = _distribution_version(distribution)
        except PackageNotFoundError:
            installed = "<missing>"
        runtime_entries.append((f"distribution:{distribution}", installed))
    for name, value in runtime_entries:
        hasher.update(_length_prefixed(name.encode("utf-8")))
        hasher.update(_length_prefixed(value.encode("utf-8")))
    entries: List[Tuple[str, bytes]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.suffix != ".py" and path.name not in _PRODUCER_RESOURCE_NAMES:
            continue
        rel = path.relative_to(root).as_posix()
        entries.append((rel, path.read_bytes()))
    entries.sort(key=lambda item: item[0])
    for rel, payload in entries:
        hasher.update(_length_prefixed(rel.encode("utf-8")))
        hasher.update(_length_prefixed(len(payload).to_bytes(8, "big")))
        hasher.update(_length_prefixed(_sha256_hex(payload).encode("ascii")))
    return hasher.hexdigest()


def _python_input_files(package_dir: Path) -> List[Path]:
    return list_indexed_python_files(package_dir)


def _python_file_records(
    package_dir: Path, files: Sequence[Path]
) -> List[Tuple[str, int, str]]:
    """Fingerprint lexical Python identities, including supported file symlinks.

    The Python extractor historically walks lexical package paths and may read
    a symlinked ``.py`` file. Resolving here would either rename that module to
    its target or reject an outside-package target, so keep the shared
    selector's lexical identity and hash the bytes read through it.
    """
    package_dir = Path(package_dir).resolve()
    records: List[Tuple[str, int, str]] = []
    seen: Dict[str, Path] = {}
    for path in files:
        lexical = Path(path)
        try:
            rel = lexical.relative_to(package_dir).as_posix()
        except ValueError as error:
            raise ValueError(
                f"Python input path {lexical} is outside package {package_dir}"
            ) from error
        prior = seen.get(rel)
        if prior is not None and prior != lexical:
            raise ValueError(
                f"input paths {prior} and {lexical} share relative path {rel}"
            )
        seen[rel] = lexical
        payload = lexical.read_bytes()
        records.append((rel, len(payload), _sha256_hex(payload)))
    return records


def _c_file_records(package_dir: Path, files: Sequence[Path]) -> List[Tuple[str, int, str]]:
    """Fingerprint the C selector's deliberately resolved, contained paths."""
    package_dir = Path(package_dir).resolve()
    records: List[Tuple[str, int, str]] = []
    seen: Dict[str, Path] = {}
    for path in files:
        resolved = Path(path).resolve()
        rel = package_relative_posix(resolved, package_dir)
        prior = seen.get(rel)
        if prior is not None and prior != resolved:
            raise ValueError(
                f"input paths {prior} and {resolved} share relative path {rel}"
            )
        seen[rel] = resolved
        payload = resolved.read_bytes()
        records.append((rel, len(payload), _sha256_hex(payload)))
    return records


def _c_compile_commands_record(package_dir: Path) -> Optional[Tuple[str, int, str]]:
    """Include compile_commands.json bytes when present, even via a symlink."""
    path = Path(package_dir).resolve() / "compile_commands.json"
    if not path.is_file():
        return None
    payload = path.read_bytes()
    return ("compile_commands.json", len(payload), _sha256_hex(payload))


def selected_input_records(indexer: str, package_dir: Path) -> List[Tuple[str, int, str]]:
    package_dir = Path(package_dir).resolve()
    if indexer == "python":
        records = _python_file_records(
            package_dir, _python_input_files(package_dir)
        )
    elif indexer == "c":
        records = _c_file_records(
            package_dir, list_indexed_c_files(package_dir)
        )
        extra = _c_compile_commands_record(package_dir)
        if extra is not None:
            records = [row for row in records if row[0] != extra[0]]
            records.append(extra)
    else:
        raise ValueError(f"unknown indexer {indexer!r}")
    records.sort(key=lambda item: item[0])
    return records


def compute_index_fingerprint(
    indexer: str,
    package_dir: Path,
    options: Mapping[str, Any],
) -> Dict[str, Any]:
    """Return a schema-valid supported ``index_input`` block."""
    if not reuse_supported(indexer, options):
        raise ValueError("compute_index_fingerprint requires a supported configuration")
    package_dir = Path(package_dir).resolve()
    records = selected_input_records(indexer, package_dir)
    producer = producer_digest()
    hasher = hashlib.sha256()
    hasher.update(_length_prefixed(INDEX_INPUT_PROTOCOL.encode("utf-8")))
    hasher.update(_length_prefixed(str(INDEX_INPUT_SCHEMA_VERSION).encode("ascii")))
    hasher.update(_length_prefixed(INDEX_INPUT_ALGORITHM.encode("ascii")))
    hasher.update(_length_prefixed(indexer.encode("ascii")))
    hasher.update(_length_prefixed(_canonical_options_bytes(options)))
    hasher.update(_length_prefixed(str(len(records)).encode("ascii")))
    for rel, size, digest in records:
        hasher.update(_length_prefixed(rel.encode("utf-8")))
        hasher.update(_length_prefixed(str(size).encode("ascii")))
        hasher.update(_length_prefixed(digest.encode("ascii")))
    hasher.update(_length_prefixed(producer.encode("ascii")))
    return {
        "schema_version": INDEX_INPUT_SCHEMA_VERSION,
        "algorithm": INDEX_INPUT_ALGORITHM,
        "indexer": indexer,
        "options": dict(options),
        "n_files": len(records),
        "producer_digest": producer,
        "digest": hasher.hexdigest(),
        "reuse_supported": True,
    }


def unsupported_index_input(
    indexer: str,
    options: Mapping[str, Any],
    *,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    bad = unsupported_reuse_options(indexer, options)
    text = reason or (
        "toolchain-dependent option(s) have no complete reuse cache key: "
        + ", ".join(bad)
    )
    return {
        "schema_version": INDEX_INPUT_SCHEMA_VERSION,
        "algorithm": INDEX_INPUT_ALGORITHM,
        "indexer": indexer,
        "options": dict(options),
        "n_files": None,
        "producer_digest": None,
        "digest": None,
        "reuse_supported": False,
        "reason": text,
    }


def validate_supported_index_input(block: Any) -> Optional[Dict[str, Any]]:
    """Return a copy if ``block`` is a strict supported fingerprint, else None."""
    if not isinstance(block, Mapping):
        return None
    if set(block) != set(SUPPORTED_BLOCK_KEYS):
        return None
    if block.get("schema_version") != INDEX_INPUT_SCHEMA_VERSION:
        return None
    if block.get("algorithm") != INDEX_INPUT_ALGORITHM:
        return None
    indexer = block.get("indexer")
    if indexer not in {"python", "c"}:
        return None
    options = block.get("options")
    expected_keys = PYTHON_OPTION_KEYS if indexer == "python" else C_OPTION_KEYS
    if not isinstance(options, Mapping) or set(options) != set(expected_keys):
        return None
    if any(not isinstance(options[key], bool) for key in expected_keys):
        return None
    if block.get("reuse_supported") is not True:
        return None
    n_files = block.get("n_files")
    if not isinstance(n_files, int) or isinstance(n_files, bool) or n_files < 0:
        return None
    producer = block.get("producer_digest")
    digest = block.get("digest")
    if not isinstance(producer, str) or not _SHA256_HEX.fullmatch(producer):
        return None
    if not isinstance(digest, str) or not _SHA256_HEX.fullmatch(digest):
        return None
    return {
        "schema_version": INDEX_INPUT_SCHEMA_VERSION,
        "algorithm": INDEX_INPUT_ALGORITHM,
        "indexer": indexer,
        "options": {key: bool(options[key]) for key in expected_keys},
        "n_files": n_files,
        "producer_digest": producer,
        "digest": digest,
        "reuse_supported": True,
    }


def _source_root_matches(manifest: Mapping[str, Any], package_dir: Path) -> bool:
    raw = manifest.get("source_root")
    if not isinstance(raw, str) or not raw:
        return False
    try:
        recorded = Path(raw).resolve()
    except (OSError, RuntimeError):
        return False
    return recorded == Path(package_dir).resolve()


def _current_snapshot_id(graph_root: Path) -> Optional[str]:
    pointer = Path(graph_root) / "current"
    try:
        if pointer.is_symlink() or not pointer.is_file():
            return None
        snap_id = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return snap_id or None


def lookup_reusable_snapshot(
    graph_root: Path,
    package_dir: Path,
    indexer: str,
    options: Mapping[str, Any],
    expected: Mapping[str, Any],
) -> Optional[Path]:
    """Return the current snapshot path on a true hit; otherwise None.

    Missing graphs, legacy/malformed blocks, invalid doctor reports, changed
    inputs, changed producer, or a different source root are misses.
    """
    from graphrag_code.byog_snapshot_graph_audit import (  # type: ignore
        SnapshotGraphAuditError,
        resolve_snapshot,
    )
    from graphrag_code.persisted_graph_doctor import (  # type: ignore
        PersistedGraphDoctorError,
        audit_graph_root,
    )
    from graphrag_code.persisted_graph_integrity import AmbiguousIndexerError

    graph_root = Path(graph_root)
    package_dir = Path(package_dir).resolve()
    if not graph_root.exists():
        return None
    try:
        snap_dir, snap_id, manifest = resolve_snapshot(graph_root, None)
    except (SnapshotGraphAuditError, OSError):
        return None
    if snap_id is None or not isinstance(manifest, Mapping):
        return None
    try:
        report = audit_graph_root(graph_root, indexer=indexer)
    except (
        PersistedGraphDoctorError,
        SnapshotGraphAuditError,
        AmbiguousIndexerError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ):
        return None
    if not report.get("ok"):
        return None
    recorded = validate_supported_index_input(manifest.get("index_input"))
    if recorded is None:
        return None
    if recorded["indexer"] != indexer:
        return None
    if recorded["options"] != dict(options):
        return None
    if recorded["digest"] != expected["digest"]:
        return None
    if recorded["producer_digest"] != expected["producer_digest"]:
        return None
    if recorded["n_files"] != expected["n_files"]:
        return None
    if not _source_root_matches(manifest, package_dir):
        return None
    if _current_snapshot_id(graph_root) != snap_id:
        return None
    return Path(snap_dir).resolve()


def raise_if_source_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> None:
    if before.get("digest") == after.get("digest") and before.get(
        "producer_digest"
    ) == after.get("producer_digest"):
        return
    print(
        "graphrag-code: source changed during indexing; published nothing",
        file=sys.stderr,
    )
    raise SystemExit(2)


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


def _acquire_exclusive_lock(fd: int) -> str:
    backend = _available_lock_backend()
    if backend == "fcntl":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)
        return backend
    if backend == "msvcrt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return backend
    raise IndexBuildLockError(
        f"cross-process index lock is unsupported on {sys.platform!r}; "
        "refusing to index without an exclusive build lock"
    )


def _release_exclusive_lock(fd: int, backend: str) -> None:
    if backend == "fcntl":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if backend == "msvcrt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextmanager
def index_build_lock(graph_root: Path) -> Iterator[None]:
    """Exclusive CLI index lock. Not the publication lock. See module docs."""
    graph_root = Path(graph_root)
    graph_root.mkdir(parents=True, exist_ok=True)
    lock_path = graph_root / INDEX_LOCK_NAME
    if lock_path.is_symlink():
        raise IndexBuildLockError(
            f"unsafe symlinked index lock is unsupported: {lock_path}"
        )
    open_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW
    try:
        before = lock_path.lstat()
    except FileNotFoundError:
        before = None
    except OSError as error:
        raise IndexBuildLockError(
            f"cannot inspect index lock {lock_path}: {error}"
        ) from error
    else:
        if stat.S_ISLNK(before.st_mode):
            raise IndexBuildLockError(
                f"unsafe symlinked index lock is unsupported: {lock_path}"
            )
    try:
        fd = os.open(str(lock_path), open_flags, 0o644)
    except OSError as error:
        if getattr(error, "errno", None) == getattr(os, "ELOOP", object()):
            raise IndexBuildLockError(
                f"unsafe symlinked index lock is unsupported: {lock_path}"
            ) from error
        raise IndexBuildLockError(
            f"cannot open index lock {lock_path}: {error}"
        ) from error
    backend: Optional[str] = None
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise IndexBuildLockError(
                f"index lock is not a regular file: {lock_path}"
            )
        if before is not None and getattr(os, "O_NOFOLLOW", 0) == 0:
            if opened.st_dev != before.st_dev or opened.st_ino != before.st_ino:
                raise IndexBuildLockError(
                    f"index lock changed while opening it: {lock_path}"
                )
        backend = _acquire_exclusive_lock(fd)
        yield
    finally:
        if backend is not None:
            try:
                _release_exclusive_lock(fd, backend)
            except OSError:
                pass
        os.close(fd)
