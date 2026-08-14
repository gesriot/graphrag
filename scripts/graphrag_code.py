#!/usr/bin/env python
"""Source-checkout compatibility entry for the product CLI.

The authoritative Typer application is :mod:`graphrag_code.cli`.
This file must not shadow the installable ``graphrag_code`` package.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    src = str(_SRC)
    if src in sys.path:
        sys.path.remove(src)
    sys.path.insert(0, src)

from graphrag_code.cli import main

if __name__ == "__main__":
    main()
