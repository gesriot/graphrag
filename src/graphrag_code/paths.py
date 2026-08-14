"""Locate packaged resources and, when present, the source checkout.

Installed wheels live under site-packages and have no checkout. An editable
src-layout install is recognized only by a specific resource contract, not by
walking parents for an unrelated pyproject.toml.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

PACKAGE_DIR = Path(__file__).resolve().parent
CHECKOUT_GATE_MANIFEST = ("scripts", "port_gates.json")
CHECKOUT_EVIDENCE_DOC = ("examples", "PORT_EVIDENCE.md")
CHECKOUT_PROJECT_FILE = "pyproject.toml"


def source_checkout_root() -> Optional[Path]:
    """Return the repository root for a src-layout checkout, else None."""
    if PACKAGE_DIR.name != "graphrag_code":
        return None
    src = PACKAGE_DIR.parent
    if src.name != "src":
        return None
    root = src.parent
    if not (root / CHECKOUT_PROJECT_FILE).is_file():
        return None
    if not (root.joinpath(*CHECKOUT_GATE_MANIFEST)).is_file():
        return None
    if not (root.joinpath(*CHECKOUT_EVIDENCE_DOC)).is_file():
        return None
    return root


def require_source_checkout(purpose: str) -> Path:
    """Return the checkout root or raise SystemExit(2) with an actionable hint."""
    root = source_checkout_root()
    if root is not None:
        return root
    print(
        f"graphrag-code: {purpose} requires a source checkout "
        "(scripts/port_gates.json and examples/PORT_EVIDENCE.md). "
        "Run it from the repository with "
        "`uv run python scripts/port_eval.py`. "
        "A wheel install does not bundle published graphs or port evidence.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def packaged_resource(name: str) -> Path:
    """Return a small runtime file shipped next to this package."""
    path = PACKAGE_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"packaged resource missing: {name}")
    return path
