"""Shared Python input selection for indexing and reuse fingerprints.

This is the indexer's real file-selection contract. Fingerprinting must call
the same functions so a second almost-equivalent ``rglob`` cannot drift.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import List


def indexable_initializer(path: Path) -> bool:
    """Whether an initializer defines an API symbol rather than re-exporting.

    Re-export-only ``__init__.py`` modules deliberately stay out of the
    graph: adding a second root entity for every imported symbol would make
    aliases look like duplicate definitions. A direct public function or
    class is different—it is an executable entry point and needs a graph
    entity (for example ``sqlparse:split``). Runtime audit coverage lives
    in ``scripts/init_api_runtime_audit.py``.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise RuntimeError(f"cannot parse initializer {path}: {exc}") from exc
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and not node.name.startswith("_")
        for node in tree.body
    )


def list_indexed_python_files(package_dir: Path) -> List[Path]:
    """Deterministic list of package ``.py`` files the Python indexer walks.

    Includes non-test ``.py`` files and only those ``__init__.py`` files that
    define a public function or class. Re-export-only initializers are
    excluded. Sort order matches the historical ``sorted(rglob)`` contract.
    """
    package_dir = Path(package_dir).resolve()
    return sorted(
        p
        for p in package_dir.rglob("*.py")
        if "tests" not in p.parts
        and (p.name != "__init__.py" or indexable_initializer(p))
    )
