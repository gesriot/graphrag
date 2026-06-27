# Provenance — vendored `charset-normalizer` (graph pipeline test target)

First real-world heuristic + data-table-heavy pure-Python library chosen to exercise the deterministic graph + context-pack + golden-contract porting rails (beyond simple parsers and data structures).

## Source
- Package: `charset-normalizer` (jawah/charset_normalizer)
- Upstream: https://github.com/jawah/charset_normalizer
- Retrieved: 2026-06-26 from shallow clone of main branch (commit at time of clone).
- Pure Python implementation (core logic only; mypyc-accelerated wheels and hooks excluded for the graph target).

## License — gate step 1 (captured)
- **MIT License**
- Full text in `LICENSE` (verbatim copy from upstream).
- Copyright © 2025 TAHRI Ahmed R. (and contributors).

## What was vendored
- The `charset_normalizer/` package (all .py modules from `src/charset_normalizer/`):
  - api.py (main from_bytes / from_path / from_fp)
  - cd.py (coherence detection)
  - md.py (mess/chaos detection)
  - constant.py (large data tables: ENCODING_MARKS, UNICODE_RANGES, language frequencies, etc.)
  - models.py, utils.py, legacy.py, __init__.py, version.py
- `LICENSE`
- `NOTICE.md` (for attribution of frequency data)
- No tests/, docs/, mypyc_hook, bin/, CI files, or generated artifacts.
- The vendored tree is importable as `charset_normalizer` when `PYTHONPATH=examples`.

## Purpose (Phase 7 / pipeline validation)
- Test graph extraction on code that is:
  - Heavily data-driven (constant.py tables → uses_data edges are critical).
  - Algorithmic (mess_ratio, coherence probes, language detection).
  - Real production library (used in requests, replaces chardet).
- Validate that context packs capture both code + the large frequency/ range tables accurately.
- Produce a clean audited graph and a reproducible behavior contract.
- Demonstrate the pipeline on a library where "raw source vs focused packs" difference matters (data tables + multiple analysis passes).

## Scope (bounded for porting gate)
- Core detection surface:
  - `from_bytes`, `from_path`, `from_fp`
  - `CharsetMatches.best()`, `CharsetMatch` properties (encoding, language, chaos, coherence, ...)
  - Supporting: mess detection, coherence, BOM/encoding mark handling.
- Out of scope for initial slice (documented boundaries):
  - Full CLI (`__main__`, bin/)
  - Legacy `detect()` wrapper details if they diverge
  - Logging handlers, mypyc specifics
  - Plugin/extension mechanisms (none in core)
  - Complete support for every rare encoding edge case (focus on stable high-frequency paths first)
- Golden contract will pin observable outputs: detected encoding, language, chaos/coherence scores, normalized text for fixed sample inputs (and/or file hashes).

## Scale notes (to be measured)
- ~5.7k LOC vendored Python.
- Large constants (constant.py >2k LOC) → expect many data entities + uses_data relationships.
- Expect good structural call graph from api → cd/md/utils (mostly direct calls + table lookups).

## Next gates (recorded before any Rust)
1. ✅ License captured.
2. Golden / behavior contract captured (see tests/ or generated golden_*.json in this tree or sibling _rust/tests) **before** writing any .rs.
3. Run `scripts/index_python.py` + `audit_call_edges.py` → pass_rate=1.0, 0 anomalies/dangling/semantic_suspicions.
4. Only then `port_eval.py` (or manual equivalent) with context packs.

This target was selected because it is:
- Pure Python
- Permissively licensed
- Has rich test data + deterministic outputs on samples
- Stresses the recent "data_dependencies" and closure improvements in the packer
- Different enough from sqlparse (heuristic + unicode tables vs token stream)

## Reproduce vendoring
```bash
git clone --depth 1 https://github.com/jawah/charset_normalizer.git /tmp/cn
mkdir -p examples/charset_normalizer
cp -r /tmp/cn/src/charset_normalizer/* examples/charset_normalizer/
cp /tmp/cn/LICENSE examples/charset_normalizer/
# (optionally copy selected data/ samples for golden)
```

Update this file with measured graph stats and golden results once gates are executed.
