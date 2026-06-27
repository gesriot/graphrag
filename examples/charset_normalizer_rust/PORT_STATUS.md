# charset_normalizer_rust — Port Readiness / Productization Status

Date: 2026-06-27

This is a scoped readiness pass for packaging, docs, CLI binary, and handoff artifacts.
It is **not** a detector implementation change.

Reference Python: `examples/charset_normalizer/` (with PROVENANCE.md, LICENSE, NOTICE.md).
Rust port: `examples/charset_normalizer_rust/`
Saved GraphRAG context packs: `examples/charset_normalizer_rust/packs/`

## What this port targets (for evaluation/handoff)

Core + product surface:
- `from_bytes` and options-aware / trace variants
- `from_path` / `from_reader` / `from_fp` (+ `_with_options*`)
- `FromBytesOptions`
- `detect_legacy` / `detect_chardet_compatible`
- `CharsetMatch` / `CharsetMatches` surface used by CLI + normalization
- Full upstream-style CLI (`normalizer`) with normalize/JSON/minimal/verbose/alternatives/threshold/preemptive/replace/force + stdin support

## Achieved on captured contracts (scoped language)

- **Golden contract (byte-exact)**: 18/18 samples in `tests/golden_detection.json` (captured from Python reference *before* any Rust code; see `tests/detection_contract.rs`).
- **Off-golden best-match assertions**: exact encoding matches on additional fixed cases.
- **Deterministic differential matrix**: 72 CLI/detector pytest items (17 fixed generated + 21 seeded + 26 adversarial payloads + fixtures/toggles). Current status: 70 pass + 2 expected xfails for adversarial inputs whose best-encoding tie-break is intentionally unstable (short_20 resolved by matching Python `str.isprintable()` on U+00A0 in mess detection).
- **Exhaustive codec/CD parity**: 6 pytest items covering all supported single-byte `encoding_languages`, multibyte language mapping, all 0x00..0xFF strict decode probes for single-byte codecs, representative multibyte strict decode probes, and representative encode/output round-trips. Current status: 4 pass + 2 expected xfails for UTF-7 (SIG/BOM strip policy per api.py vs raw stdlib) and euc_jis_2004 (extension vs encoding_rs profile). Single-byte codecs exact; most MB via encoding_rs/custom Korean/HZ/UTF special handling; rare MB table variants documented.
- **CLI byte-exact / snapshot**: non-verbose JSON, minimal output, argparse errors, paths (abs), stdin, normalize side-effects (written bytes), replace/force flows.
- **Normalized verbose trace parity**: when `explain=true` (or `--verbose`), trace events from api/md match in content (timestamps, some floats/sets masked for determinism; not raw log strings).

**Total Rust tests**: 81 (56 unit parity in cd/models/md/codec/API + 9 CLI + 3 contract/golden + 13 off-golden/large-lazy). All pass; 0 ignored.

## Byte-exact vs. normalized parity (precise)

- Byte-exact: golden JSON, off-golden best assertions, CLI non-verbose output + normalize output bytes, test matrix best-encoding on stable cases.
- Normalized parity (traces): key detection events (e.g. "passed initial chaos probing", "definitive match", fallback, fast-track, skip reasons) with structural/fuzzy matching suitable for explain-mode debugging.
- Do not expect byte-identical verbose logs or floating point text.

## Intentional non-parity / design differences (not bugs)

- No global `set_logging_handler` / side-effect logging setup. Use `from_*_with_options_and_trace(..., explain=true)` (returns `Vec<String>`) or `--verbose` (emits to stderr with fixed ts for tests).
- `detect(&[u8])` returns modern best `CharsetMatch` (simple path). Python top-level `detect` is the legacy wrapper. Rust equivalents: `detect_legacy(byte_str, should_rename_legacy: bool)` and `detect_chardet_compatible(byte_str)`.
- `FromBytesOptions::explain` controls trace collection (no logger mutation).
- Rust CLI accepts `--cp-isolation` / `--cp-exclusion` as additive parity/test harness extensions wired to `FromBytesOptions`. The vendored Python CLI does not expose these flags, so Rust intentionally keeps them out of `--help` to preserve byte-exact shared help snapshots.
- Small differences in error message text or Python-only type paths are expected.
- Legacy post-processing (small-sample confidence adjust, utf_8_sig mapping, CHARDET_CORRESPONDENCE) lives only in the `detect_legacy*` fns.
- Codec contract for detection: matches Python api.py (SIG/BOM stripping per should_strip_sig_or_bom except utf16/utf32; utf7 special full-decode-then-strip). Raw stdlib codecs.decode may differ for utf7 (keeps U+FEFF) and certain MB table variants (big5*/euc_jis_2004 extensions); Rust uses custom only for utf7/hz/johab/iso2022_kr + encoding_rs for most other MB. See KNOWN_XFAIL in parity test.

## Test commands (current counts as of this status)

From inside Rust dir:
```bash
cargo fmt
cargo test --quiet   # expects 81 passing tests (see breakdown above)
```

From repo root:
```bash
PYTHONPATH=. uv run pytest examples -q --tb=no
# expected current summary: 440 passed, 4 xfailed
# xfails are documented adversarial detector (bom8_badcont, short_high) + codec-policy (utf7 policy-vs-raw, euc_jis_2004) cases
# (short_20 xfail burned down via narrow is_printable fix matching Python source)
# MB codec note: single-byte codecs exact; most MB via encoding_rs/custom Korean/HZ/UTF special handling; rare MB table variants documented.
```

Handoff/CI wrapper (recommended for repeatable verification; runs fmt+test+targeted, optional --full/--scale):
```bash
examples/charset_normalizer_rust/tools/check_port.sh
examples/charset_normalizer_rust/tools/check_port.sh --full
examples/charset_normalizer_rust/tools/check_port.sh --scale
# (no network; scale is opt-in and excluded by default)
```

Opt-in scale harness (100k+ payloads, release timings; excluded from default):
```bash
CN_SCALE=1 PYTHONPATH=. uv run python examples/charset_normalizer_rust/tools/scale_harness.py
```

CLI smoke (inside dir):
```bash
cargo run --bin normalizer -- tests/data/sample-french-1.txt
cargo run --bin normalizer -- --minimal -- tests/data/sample-french-1.txt
cargo run --bin normalizer -- --normalize tests/data/sample-french-1.txt
```

GraphRAG context (from repo root):
```bash
uv run python scripts/context_pack.py "__main__:cli_detect" --graph byog_charset_normalizer --full-text
uv run python scripts/context_pack.py "api:from_bytes" --graph byog_charset_normalizer --full-text
```

## Generated tables / artifacts — regeneration instructions

- Single-byte + special codecs (`src/python_codecs.rs`, `src/korean_codecs.rs`):
  ```bash
  python3 tools/generate_codecs.py && cargo fmt
  ```
  (Tool docstring and generated file headers state source.)

- `tests/golden_detection.json` + contract: captured pre-implementation from Python reference on fixed samples. Regenerate only if intentionally refreshing the contract (run Python to produce new json, update contract test expectations if changed).

- Scale harness / differential generators: deterministic (fixed seeds); see file headers and docstrings.

All regeneration commands are documented in this file, README.md, and tool sources.

## Vendoring / provenance / license

- MIT (matches upstream). Full text vendored at `../charset_normalizer/LICENSE`.
- See `../charset_normalizer/PROVENANCE.md` and `NOTICE.md` for retrieval date, what was included, and attribution.
- Rust crate: `publish = false`; this is an evaluation port inside the graphrag examples tree.
- References: https://github.com/jawah/charset_normalizer

## Remaining handoff caveats (P2/P3)

- Ambiguous single-byte/adversarial cases can produce ranking differences; pinned unstable cases are marked as expected xfails with source-backed reasons.
- Default test surface includes exhaustive CD and single-byte codec probes plus representative multibyte probes, but not broad random fuzz or exhaustive multibyte variant tables (100k+ scale is opt-in only). Single-byte exact via generated tables; most multibyte via encoding_rs (with documented rare table/extension diffs for big5*/euc_jis_2004/iso2022_jp* variants vs py stdlib codecs).
- No claim of "full upstream parity" or complete feature match. Readiness is scoped to golden contract + differential matrix + product CLI surface on the captured inputs.
- Integration with external callers (beyond the provided Python differential and CLI snapshots) should be validated with target workloads.
- The port prioritizes observable behavior on golden + generated cases; internal structure follows the contract packs rather than line-by-line port.

## Quick readiness checklist

- [x] Cargo metadata + [[bin]] name="normalizer" (product CLI)
- [x] README covers required APIs + regeneration + CLI + parity language
- [x] PORT_STATUS.md (this file) present with byte-exact / parity / non-parity / commands / caveats
- [x] Generated artifacts have documented regen steps
- [x] `cargo test` passes and `pytest examples` completes with documented expected xfails (see commands above)
- [x] Scoped language throughout (no overclaim)

For full context packs and original Python sources used in porting, rerun the `context_pack.py` commands listed above.
The saved JSON packs under `packs/` are the handoff snapshot used during this port; rerunning `context_pack.py` against a fresh or local `byog_charset_normalizer` graph is the source of truth if extractor behavior changes.

This file + updated README + Cargo.toml constitute the productization/readiness artifacts.
