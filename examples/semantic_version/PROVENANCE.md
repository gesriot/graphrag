# Provenance — vendored `semantic_version` (third Python→Rust porting target)

This is the **first genuinely external** porting target (mini_game and mini_lang
were our own code). Vendored so indexing / golden / port are reproducible.

## Source
- Package: `semantic_version` 2.10.0 (PyPI)
- Upstream: https://github.com/rbarrois/python-semanticversion
- Retrieved: 2026-06-16 from the PyPI wheel `semantic_version-2.10.0-py2.py3-none-any.whl`
- Pure Python, no C extensions; `base.py` imports only stdlib (`functools`, `re`, `warnings`).

## License — gate step 1 (captured)
- **BSD-2-Clause** (`License :: OSI Approved :: BSD License`). Full text in `LICENSE` (verbatim).
- Redistribution permitted with the copyright notice retained, which this directory does.

## What was vendored / modified
- `base.py` — **verbatim** upstream (1449 LOC). Contains the port target (`Version`)
  plus the v2-scope spec/range machinery (`SimpleSpec`, `NpmSpec`, `BaseSpec`, ...).
- `__init__.py` — **adapted**: the upstream `importlib.metadata.version(...)` /
  `pkg_resources` lookup (needs the package pip-installed) is replaced with a static
  `__version__ = "2.10.0"`. Re-exports are unchanged.
- `django_fields.py` — **omitted**: Django ORM integration, out of scope and would
  pull in Django (not pure-stdlib).

## Port scope
- **v1 (complete):** `Version` — parse / stringify / compare / ordering / invalid
  inputs (with its identifier-comparison helpers plus `compare` / `validate`).
- **v2a (complete):** `SimpleSpec` match / select / filter and invalid-spec
  behavior.
- **v2b (complete):** `NpmSpec` match / select / filter and invalid-spec behavior.

The shared contract contains 147 golden cases across 13 files, all consumed by
the Rust integration tests. This is the complete **core porting scope**, not a
claim of full Python package API compatibility. Deliberately out of scope are
the deprecated `SpecItem` and `LegacySpec` / `Spec` compatibility APIs,
top-level `match`, full Clause equality/hash/iteration behavior, warnings and
Python-specific representations, and the omitted Django integration.
