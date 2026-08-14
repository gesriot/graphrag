#!/usr/bin/env python
"""Collision-safe package-relative C module identities.

tree-sitter-c extraction and the optional compiler dependency overlay share
one path -> module-key mapping so file, symbol, document, text-unit, call, and
depends_on identities cannot silently collapse when two directories both
contain ``util.c``.

Policy
------
1. Index every package ``.c`` / ``.h`` file once.
2. Group files by ``path.stem``.
3. If a stem occurs under only one parent directory, the module key is that
   stem (legacy-compatible: ``cJSON.c`` + ``cJSON.h`` → ``cJSON``).
4. If the same stem occurs under multiple parents, each parent group uses the
   package-relative POSIX path of ``parent/stem`` (no suffix):

       src/left/util.c  → src/left/util
       src/left/util.h  → src/left/util
       src/right/util.c → src/right/util

File titles remain ``{module_key}:{filename}``. Symbol titles remain the
legacy ``{module_key}:{name}`` when exactly one entity kind uses that name
under the module key. When two or more of ``function`` / ``struct`` /
``enum`` / ``typedef`` share a bare name under one module key, every
colliding kind is rendered as ``{module_key}:{entity_kind}:{name}`` (no
arbitrary legacy winner). Entity IDs embed the full title; callers must
never use a lossy slug as the sole unique component of a secondary identity.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

INDEXED_SUFFIXES = frozenset({".c", ".h"})


def list_indexed_c_files(package_dir: Path) -> List[Path]:
    """Deterministic list of package ``.c`` / ``.h`` files (resolved paths).

    Sort order matches historical ``sorted(package_dir.rglob(...))`` Path
    ordering (directory components before same-prefix files) so non-colliding
    packages keep stable ``human_readable_id`` sequences.
    """
    package_dir = Path(package_dir).resolve()
    files: List[Path] = []
    lexical_by_resolved: Dict[Path, Path] = {}
    for path in sorted(package_dir.rglob("*")):
        if not path.is_file() or path.suffix not in INDEXED_SUFFIXES:
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(package_dir)
        except ValueError as e:
            raise ValueError(
                f"indexed C path {path} resolves outside package {package_dir}: "
                f"{resolved}"
            ) from e
        prior = lexical_by_resolved.get(resolved)
        if prior is not None and prior != path:
            raise ValueError(
                f"indexed C paths {prior} and {path} resolve to the same file "
                f"{resolved}; refuse silent identity aliasing"
            )
        lexical_by_resolved[resolved] = path
        files.append(resolved)
    return files


def package_relative_posix(path: Path, package_dir: Path) -> str:
    """Return a package-relative POSIX path; reject outside-package paths."""
    package_dir = Path(package_dir).resolve()
    path = Path(path).resolve()
    try:
        return path.relative_to(package_dir).as_posix()
    except ValueError as e:
        raise ValueError(
            f"C identity path {path} is outside package {package_dir}"
        ) from e


def build_module_key_map(
    package_dir: Path,
    files: Optional[Sequence[Path]] = None,
) -> Dict[Path, str]:
    """Map resolved file path -> collision-safe module key.

    Raises ``ValueError`` if two files would still share a module key while
    living in different parent directories after disambiguation (should not
    happen under the path-based rule).
    """
    package_dir = Path(package_dir).resolve()
    paths = [
        Path(p).resolve()
        for p in (
            files if files is not None else list_indexed_c_files(package_dir)
        )
    ]
    if len(paths) != len(set(paths)):
        raise ValueError("duplicate resolved paths in C package identity map")
    for path in paths:
        try:
            path.relative_to(package_dir)
        except ValueError as e:
            raise ValueError(
                f"C identity path {path} is outside package {package_dir}"
            ) from e

    # stem -> set of parent directory resolved paths that contain that stem
    stem_parents: Dict[str, set[Path]] = {}
    for path in paths:
        stem_parents.setdefault(path.stem, set()).add(path.parent.resolve())

    # For each (stem, parent), decide the module key once.
    key_for_parent_stem: Dict[Tuple[Path, str], str] = {}
    for stem, parents in stem_parents.items():
        if len(parents) == 1:
            key = stem  # legacy-compatible single-parent stem
            for parent in parents:
                key_for_parent_stem[(parent, stem)] = key
        else:
            for parent in parents:
                # parent/stem as package-relative POSIX without suffix
                parent_rel = package_relative_posix(parent, package_dir)
                if parent_rel in ("", "."):
                    key = stem
                else:
                    key = f"{parent_rel}/{stem}"
                key_for_parent_stem[(parent, stem)] = key

    out: Dict[Path, str] = {}
    # Detect accidental key collisions across different parent groups.
    key_to_parents: Dict[str, set[Path]] = {}
    for path in paths:
        parent = path.parent.resolve()
        key = key_for_parent_stem[(parent, path.stem)]
        out[path] = key
        key_to_parents.setdefault(key, set()).add(parent)

    for key, parents in key_to_parents.items():
        if len(parents) > 1:
            raise ValueError(
                f"C module key {key!r} still collides across parents "
                f"{sorted(str(p) for p in parents)}; refuse silent identity collapse"
            )
    return out


def file_entity_title(path: Path, module_key: str) -> str:
    """File entity title: ``{module_key}:{filename}``."""
    return f"{module_key}:{Path(path).name}"


def symbol_entity_title(module_key: str, symbol: str) -> str:
    """Legacy symbol title when no cross-kind collision: ``{module_key}:{symbol}``."""
    return f"{module_key}:{symbol}"


# Graph entity kinds that participate in C symbol identity (not files).
SYMBOL_ENTITY_KINDS = frozenset({"function", "struct", "enum", "typedef"})


def symbol_entity_title_qualified(
    module_key: str, entity_kind: str, symbol: str
) -> str:
    """Kind-qualified symbol title: ``{module_key}:{entity_kind}:{symbol}``."""
    if entity_kind not in SYMBOL_ENTITY_KINDS:
        raise ValueError(
            f"unsupported C symbol entity kind {entity_kind!r}; expected one of "
            f"{sorted(SYMBOL_ENTITY_KINDS)}"
        )
    if not symbol:
        raise ValueError("C symbol name must be non-empty")
    return f"{module_key}:{entity_kind}:{symbol}"


def build_symbol_title_map(
    candidates: Sequence[Tuple[str, str, str]],
) -> Dict[Tuple[str, str, str], str]:
    """Map ``(module_key, entity_kind, name)`` → deterministic symbol title.

    Cross-kind collision policy (within one module key, same bare C name):
      * if exactly one entity kind uses the name → legacy ``module_key:name``;
      * if two or more kinds share the name → every colliding kind is
        ``module_key:entity_kind:name`` (no arbitrary legacy winner).

    Same-kind redeclarations are not cross-kind collisions; callers should
    dedupe candidates to unique ``(module_key, kind, name)`` before use.
    """
    by_mod_name: Dict[Tuple[str, str], set[str]] = {}
    unique: set[Tuple[str, str, str]] = set()
    for module_key, entity_kind, name in candidates:
        if entity_kind not in SYMBOL_ENTITY_KINDS:
            raise ValueError(
                f"unsupported C symbol entity kind {entity_kind!r}"
            )
        if not name:
            raise ValueError("C symbol name must be non-empty")
        if not module_key:
            raise ValueError("C module key must be non-empty")
        key = (module_key, entity_kind, name)
        unique.add(key)
        by_mod_name.setdefault((module_key, name), set()).add(entity_kind)

    out: Dict[Tuple[str, str, str], str] = {}
    # Preserve deterministic insertion order even when callers supply an
    # unordered collection or a different discovery order.
    for module_key, entity_kind, name in sorted(unique):
        kinds = by_mod_name[(module_key, name)]
        if len(kinds) > 1:
            out[(module_key, entity_kind, name)] = symbol_entity_title_qualified(
                module_key, entity_kind, name
            )
        else:
            out[(module_key, entity_kind, name)] = symbol_entity_title(
                module_key, name
            )
    return out


def module_name_is_cross_kind_collision(
    title_map: Mapping[Tuple[str, str, str], str],
    module_key: str,
    name: str,
) -> bool:
    """True when ``module_key``+``name`` has titles from more than one kind."""
    kinds = {
        kind
        for (mk, kind, nm) in title_map
        if mk == module_key and nm == name
    }
    return len(kinds) > 1


def contains_relationship_id(
    module_key: str,
    name: str,
    *,
    entity_kind: str,
    cross_kind_collision: bool,
) -> str:
    """``contains`` relationship id; kind-qualify only on cross-kind collision."""
    if entity_kind not in SYMBOL_ENTITY_KINDS:
        raise ValueError(
            f"unsupported C symbol entity kind {entity_kind!r}"
        )
    if cross_kind_collision:
        return f"rel:contains:{module_key}:{entity_kind}:{name}"
    return f"rel:contains:{module_key}:{name}"


def file_title_map(
    package_dir: Path,
    module_keys: Optional[Mapping[Path, str]] = None,
) -> Dict[Path, str]:
    """Map resolved path -> file entity title using the shared module keys."""
    package_dir = Path(package_dir).resolve()
    keys = (
        dict(module_keys)
        if module_keys is not None
        else build_module_key_map(package_dir)
    )
    return {path: file_entity_title(path, key) for path, key in keys.items()}
