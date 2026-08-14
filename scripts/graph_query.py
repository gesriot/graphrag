#!/usr/bin/env python
"""Source-checkout compatibility shim for ``graph_query``.

The implementation lives in :mod:`graphrag_code.graph_query`. Importing this
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
    from graphrag_code import graph_query as _impl
    sys.modules[__name__] = _impl
else:
    runpy.run_module("graphrag_code.graph_query", run_name="__main__", alter_sys=True)
