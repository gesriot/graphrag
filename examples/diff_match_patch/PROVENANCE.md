# Provenance — vendored `diff-match-patch` (fourth Python→Rust porting target)

Fourth porting target, chosen to broaden the evidence base beyond the parser
domain (mini_lang + semantic_version's two spec dialects): a different class of
algorithm — Myers diff, Bitap fuzzy match, and patch make/apply.

## Source
- Package: `diff-match-patch` 20241021 (PyPI) — a maintained repackaging of
  Google's Diff-Match-Patch libraries.
- Upstream: https://github.com/diff-match-patch-python/diff-match-patch
- Retrieved: 2026-06-17 from the PyPI wheel `diff_match_patch-20241021-py3-none-any.whl`
- Pure Python; `diff_match_patch.py` imports only stdlib (`re`, `sys`, `time`, `urllib.parse`).

## License — gate step 1 (captured)
- **Apache-2.0** (`License :: OSI Approved :: Apache Software License`). Full text in `LICENSE` (verbatim).

## What was vendored / modified
- `diff_match_patch.py` — **verbatim** upstream (2022 LOC): the single
  `diff_match_patch` class (diff / match / patch) plus `patch_obj`.
- `__init__.py`, `__version__.py` — **verbatim** (no install-time machinery; both
  import cleanly without the package being pip-installed).
- `tests/` — **omitted**: upstream's own test suite, not part of the port target.

## Port scope (staged, same gate per stage)
- **v1 (diff):** `diff_main` + cleanups (`diff_cleanupSemantic` / `Efficiency` /
  `Merge`) with `Diff_Timeout = 0` for determinism. golden = (text1, text2) -> diff ops.
- **v2 (match):** `match_main` (Bitap), with location / distance / threshold edge cases.
- **v3 (patch):** `patch_make` / `patch_apply` / `patch_toText` / `patch_fromText`,
  including application against imperfect source.

Each stage: license/provenance (done) → golden before Rust → `audit_call_edges`
clean → Rust port → `port_eval`.
