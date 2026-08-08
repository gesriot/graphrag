#!/usr/bin/env python
"""Shared compile-database + GNU/Clang compiler helpers for C overlays.

Used by the flattened TU-dependency overlay (``c_compiler_facts``) and the
direct include-hierarchy overlay (``c_compiler_includes``). Keeps path/argv
resolution, compiler selection, and compile DB loading in one place.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from c_identities import (  # type: ignore
    build_module_key_map,
    file_title_map,
    list_indexed_c_files,
)
from c_preprocessor import (  # type: ignore
    _compiler_identity,
    _load_compile_command_entries,
    compile_entry_argv,
    ensure_source_on_argv,
    resolve_compile_entry_cwd,
    resolve_compile_entry_source,
    split_compile_entry_args,
    strip_compile_output_flags,
)

_SUPPORTED_COMPILER = re.compile(
    r"(?:.+-)?(?:clang|gcc|cc)(?:-\d+(?:\.\d+)*)?\Z"
)


class CompilerOverlayError(RuntimeError):
    """Base error for optional compiler-backed C graph overlays."""


def compile_commands_digest(package_dir: Path) -> str:
    """Stable SHA-256 of the package's compile_commands.json bytes."""
    cc_path = Path(package_dir) / "compile_commands.json"
    raw = cc_path.read_bytes()
    return hashlib.sha256(raw).hexdigest()


def load_compile_entries(package_dir: Path) -> Tuple[List[Any], str]:
    """Load compile_commands.json entries and digest; fail closed on I/O/empty."""
    package_dir = Path(package_dir).resolve()
    cc_path = package_dir / "compile_commands.json"
    if not cc_path.is_file():
        raise CompilerOverlayError(
            f"compile_commands.json not found under {package_dir}; "
            "compiler overlay requires a compile database"
        )
    try:
        entries = _load_compile_command_entries(package_dir)
        digest = compile_commands_digest(package_dir)
    except (OSError, UnicodeError) as e:
        raise CompilerOverlayError(
            f"cannot read compile_commands.json under {package_dir}: {e}"
        ) from e
    if not entries:
        raise CompilerOverlayError(
            f"compile_commands.json under {package_dir} is empty or unreadable"
        )
    return entries, digest


def validate_compile_entry(entry: Any, entry_index: int) -> Dict[str, Any]:
    """Require a compile_commands object entry."""
    if not isinstance(entry, dict):
        raise CompilerOverlayError(
            "invalid compile_commands.json: "
            f"entry {entry_index} is not an object"
        )
    return entry


def resolve_compiler_path(token: str, *, cwd: Path) -> str:
    """Resolve one GNU/Clang-compatible compiler token or fail explicitly."""
    name = Path(token).name
    if not _SUPPORTED_COMPILER.fullmatch(name):
        raise CompilerOverlayError(
            f"unsupported compiler command {token!r}; expected clang/cc/gcc "
            "(compiler wrappers and MSVC are not supported)"
        )
    path = Path(token)
    if path.is_absolute():
        resolved = path
    elif path.parent != Path("."):
        resolved = (cwd / path).resolve()
    else:
        found = shutil.which(token)
        if not found:
            raise CompilerOverlayError(
                f"compiler from compile_commands.json is not on PATH: {token!r}"
            )
        resolved = Path(found).resolve()
    if not resolved.is_file():
        raise CompilerOverlayError(f"compiler does not exist: {resolved}")
    return str(resolved)


def compiler_from_entry(entry: Dict[str, Any], *, cwd: Path) -> str:
    """Resolve the compiler actually named by a compile database entry."""
    argv = compile_entry_argv(entry)
    if not argv:
        raise CompilerOverlayError(
            "compile_commands entry has neither arguments nor command"
        )
    return resolve_compiler_path(argv[0], cwd=cwd)


def compiler_identity(compiler: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (id_line, version) from ``compiler --version``."""
    return _compiler_identity(compiler)


def prepare_compile_entry(
    entry: Dict[str, Any],
    *,
    package_dir: Path,
) -> Tuple[Path, List[str], Path]:
    """Resolve cwd, cleaned argv (no compiler token), and primary source.

    Strips output/compile-only/dependency flags. Rejects response files.
    """
    package_dir = Path(package_dir).resolve()
    cwd = resolve_compile_entry_cwd(entry, package_dir)
    cleaned = strip_compile_output_flags(split_compile_entry_args(entry))
    if any(arg.startswith("@") for arg in cleaned):
        raise CompilerOverlayError(
            "response-file compile arguments are unsupported because hidden "
            "output flags cannot be audited safely"
        )
    src_path = resolve_compile_entry_source(
        entry, cwd=cwd, package_dir=package_dir
    )
    cleaned = ensure_source_on_argv(cleaned, src_path, cwd)
    return cwd, cleaned, src_path


def indexed_package_files(package_dir: Path) -> Dict[Path, str]:
    """Map resolved package path -> file-entity title (shared identity map)."""
    package_dir = Path(package_dir).resolve()
    files = list_indexed_c_files(package_dir)
    module_keys = build_module_key_map(package_dir, files)
    return file_title_map(package_dir, module_keys)


def path_is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def next_human_readable_id(relationships: Sequence[Dict[str, Any]]) -> int:
    max_id = 0
    for r in relationships:
        try:
            max_id = max(max_id, int(r.get("human_readable_id") or 0))
        except (TypeError, ValueError):
            continue
    return max_id + 1


def build_disabled_overlay_provenance() -> Dict[str, Any]:
    """Stable off-block for snapshot manifests."""
    return {
        "mode": "off",
        "enabled": False,
        "n_facts": 0,
        "n_translation_units": 0,
    }


CONFIDENCE_BOUNDARY = (
    "confidence=1.0 and is_deterministic=true mean the fact is "
    "re-derivable from the recorded compiler + compile_commands.json "
    "configuration, not that it is a direct textual #include or pure "
    "syntax fact independent of the toolchain."
)
