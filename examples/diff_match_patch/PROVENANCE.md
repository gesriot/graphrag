# Provenance — vendored `diff-match-patch` (fourth Python→Rust porting target)

Fourth porting target, chosen to broaden the evidence base beyond the parser
domain (mini_lang + semantic_version's two spec dialects): a different class of
algorithm — Myers diff, Bitap fuzzy match, and patch make/apply.

## Source
- Package: `diff-match-patch` 20241021 (PyPI) — a maintained repackaging of
  Google's Diff-Match-Patch libraries.
- Upstream: https://github.com/diff-match-patch-python/diff-match-patch
- Retrieved: 2026-06-17 from the PyPI wheel `diff_match_patch-20241021-py3-none-any.whl`
- Wheel SHA-256: `93cea333fb8b2bc0d181b0de5e16df50dd344ce64828226bda07728818936782`
- Pure Python; `diff_match_patch.py` imports only stdlib (`re`, `sys`, `time`, `urllib.parse`).

## License — gate step 1 (captured)
- **Apache-2.0** (`License :: OSI Approved :: Apache Software License`). Full text in `LICENSE` (verbatim).

## What was vendored / modified
- `diff_match_patch.py` — **verbatim** upstream (2022 LOC): the single
  `diff_match_patch` class (diff / match / patch) plus `patch_obj`.
- `__init__.py`, `__version__.py` — **verbatim** (no install-time machinery; both
  import cleanly without the package being pip-installed).
- `tests/` — **omitted**: upstream's own test suite, not part of the port target.

Vendored-file SHA-256 values (verified directly against the wheel):
- `LICENSE`: `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`
- `__init__.py`: `8ca504bf886e3b7eba9ffde6e9cf2b7a58eb51a8cad46eae6a13e73cb88dd0ca`
- `__version__.py`: `681b782269688ffecde257236d62e0fe242001a01002fb3c3a814d58f0f10bcb`
- `diff_match_patch.py`: `c5aecc2945441f53cdfe413b59c23e88eb43bfa61e444ca8ff0eee5823232a71`

## Port scope (staged, same gate per stage)
- **v1 (diff, complete):** `diff_main` + cleanups (`diff_cleanupSemantic` /
  `Efficiency` / `Merge`) with `Diff_Timeout = 0` for determinism; 44 golden
  cases and `overall_pass=True` with 0 recorded manual fixes.
- **v2 (match, next):** `match_main` (Bitap), with location / distance /
  threshold edge cases.
- **v3 (patch):** `patch_make` / `patch_apply` / `patch_toText` / `patch_fromText`,
  including application against imperfect source.

Each stage: license/provenance (done) → golden before Rust → `audit_call_edges`
clean → Rust port → `port_eval`.
