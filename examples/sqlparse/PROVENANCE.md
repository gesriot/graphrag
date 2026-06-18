# Provenance — vendored `sqlparse` (Phase 5 scale experiment)

First large, multi-package scale target (Plan Phase 5). Earlier targets were 1-3
files / a single class; sqlparse is a real ~4.1k-LOC project with nested
sub-packages (`engine/`, `filters/`), exercising cross-module/cross-package
resolution at scale rather than one-file complexity.

## Source
- Package: `sqlparse` 0.5.5 (PyPI) — a non-validating SQL parser/formatter.
- Upstream: https://github.com/andialbrecht/sqlparse
- Retrieved: 2026-06-18 from the PyPI wheel `sqlparse-0.5.5-py3-none-any.whl`.
- Pure Python, no runtime dependencies.

## License — gate step 1 (captured)
- **BSD-3-Clause** (`License :: OSI Approved :: BSD License`). Full text in `LICENSE` (verbatim).

## What was vendored
- The full `sqlparse/` package (21 modules incl. `engine/` and `filters/`
  sub-packages), verbatim, plus `LICENSE`.
- `__pycache__` removed; no source modifications.

## Purpose (staged)
1. **Scale measurement first:** index + `audit_call_edges` at scale; record LOC,
   timing, graph sizes; classify any new false-edge classes / recall gaps from
   cross-package imports before porting anything.
2. **Then** select one cohesive component (likely the tokenizer + lexer/filter
   pipeline) to port end-to-end with a differential SQL corpus.
