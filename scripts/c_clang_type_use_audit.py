#!/usr/bin/env python
"""Source-checkout compatibility shim for ``c_clang_type_use_audit``.

The implementation lives in :mod:`graphrag_code.c_clang_type_use_audit`. Importing this
file yields the same module object as the packaged implementation.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    src = str(_SRC)
    if src in sys.path:
        sys.path.remove(src)
    sys.path.insert(0, src)

if __name__ != "__main__":
    from graphrag_code import c_clang_type_use_audit as _impl
    sys.modules[__name__] = _impl
else:
    runpy.run_module("graphrag_code.c_clang_type_use_audit", run_name="__main__", alter_sys=True)
