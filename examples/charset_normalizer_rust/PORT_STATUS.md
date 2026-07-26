# charset_normalizer_rust — Port Readiness / Productization Status

Date: 2026-07-25

This records a scoped readiness pass for packaging, docs, CLI parity, and handoff artifacts.
It does not change the detector algorithm.

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
- **Fixed/adversarial differential matrix**: 72 CLI/detector pytest items (17 fixed generated + 21 earlier seeded + 26 adversarial payloads + fixtures/toggles). Current status: 70 pass + 2 expected xfails for adversarial inputs whose best-encoding tie-break is intentionally unstable (short_20 resolved by matching Python `str.isprintable()` on U+00A0 in mess detection).
- **Seeded live differential harness**: Python is executed as the oracle on each run; Rust is observed through its own strict decoder, rather than re-decoding Rust's selected label in Python. The default `check_port.sh` corpus has 79 generated inputs across 15 source-encoding categories (real UTF-8/BOM, UTF-16 LE/BE, Latin-1, CP1251, Shift-JIS, GB2312, truncations, invalid/mixed runs, empty/single/very-short, multi-valid, and seeded mutations). The on-demand full corpus has 530 inputs, adding all 256 one-byte values, 240 seeded mutations, and long text. Seed `20260725`: 79/79 and 530/530 strong agreements, respectively; no divergences were suppressed. The harness initially exposed two port bugs: a tie-order mismatch on the 1,584-byte `real_utf16le` input (Python `Swedish`, Rust `German`) caused by a Rust-only language-order tie-break and hash-map merge, and an over-broad `English` inference for the one-byte `00`/`e3` and five-byte `4100420043` inputs. The port now preserves Python's stable candidate order and infers English only for an actual `ascii` match; both findings are covered by Rust regression tests and the live sweep.
- **Exhaustive codec/CD parity**: 6 pytest items covering all supported single-byte `encoding_languages`, multibyte language mapping, all 0x00..0xFF strict decode probes for single-byte codecs, representative multibyte strict decode probes, and representative encode/output round-trips. Current status: 4 pass + 2 expected xfails for UTF-7 (SIG/BOM strip policy per api.py vs raw stdlib) and euc_jis_2004 (extension vs encoding_rs profile). Single-byte codecs exact; most MB via encoding_rs/custom Korean/HZ/UTF special handling; rare MB table variants documented.
- **CLI byte-exact / snapshot**: non-verbose JSON, minimal output, argparse errors, paths (abs), stdin, normalize side-effects (written bytes), replace/force flows.
- **Normalized verbose trace parity**: when `explain=true` (or `--verbose`), trace events from api/md match in content (timestamps, some floats/sets masked for determinism; not raw log strings).

**Total Rust tests**: 82 (57 unit parity in cd/models/md/codec/API + 9 CLI + 3 contract/golden + 13 off-golden/large-lazy). All pass; 0 ignored.

## Byte-exact vs. normalized parity (precise)

- Byte-exact: golden JSON, off-golden best assertions, CLI non-verbose output + normalize output bytes, test matrix best-encoding on stable cases.
- Normalized parity (traces): key detection events (e.g. "passed initial chaos probing", "definitive match", fallback, fast-track, skip reasons) with structural/fuzzy matching suitable for explain-mode debugging.
- Do not expect byte-identical verbose logs or floating point text.

### Differential agreement policy

The seeded harness compares the canonical best encoding, strictly decoded Unicode text, language, BOM flag, and chaos/coherence scores (each within 0.005) on live Python and Rust observations. A Python result is *confident* only when the payload is at least 32 bytes, its chaos is at most 0.05, and the sorted result has one de-duplicated candidate. A confident difference fails the default `check_port.sh` gate. Tiny, noisy, or multi-candidate cases use the same comparison but are printed as explicit ambiguous findings, with the payload and decoded-text fingerprints needed to reproduce them; they are never silently counted as agreement. The current two historical detector xfails are still retained in the fixed adversarial matrix and are not covered up by this policy.

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
cargo test --quiet   # expects 82 passing tests (see breakdown above)
```

From repo root:
```bash
PYTHONPATH=. uv run pytest examples -q --tb=no
# expected current summary: 445 passed, 4 xfailed
# xfails are documented adversarial detector (bom8_badcont, short_high) + codec-policy (utf7 policy-vs-raw, euc_jis_2004) cases
# (short_20 xfail burned down via narrow is_printable fix matching Python source)
# MB codec note: single-byte codecs exact; most MB via encoding_rs/custom Korean/HZ/UTF special handling; rare MB table variants documented.
```

Handoff/CI wrapper (recommended for repeatable verification; runs fmt+test+targeted, optional --full/--scale):
```bash
examples/charset_normalizer_rust/tools/check_port.sh
examples/charset_normalizer_rust/tools/check_port.sh --full
examples/charset_normalizer_rust/tools/check_port.sh --scale
examples/charset_normalizer_rust/tools/check_port.sh --differential-full
# (no network; scale is opt-in and excluded by default)
```

Seeded Python-oracle detector differential (the default corpus is part of `check_port.sh`):
```bash
PYTHONPATH=. uv run python examples/charset_normalizer_rust/tools/differential_harness.py --assert-clean
# seed=20260725: 79 inputs / 15 source-encoding categories / 79 agreements / 0 disagreements
PYTHONPATH=. uv run python examples/charset_normalizer_rust/tools/differential_harness.py --full --assert-clean
# seed=20260725: 530 inputs / 15 source-encoding categories / 530 agreements / 0 disagreements
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

- Scale harness and seeded differential generators: deterministic (fixed seeds); see file headers and docstrings. The default differential corpus is CI-bounded; `differential_harness.py --full` is the longer every-byte/mutation/long-input sweep.

All regeneration commands are documented in this file, README.md, and tool sources.

## Vendoring / provenance / license

- MIT (matches upstream). Full text vendored at `../charset_normalizer/LICENSE`.
- See `../charset_normalizer/PROVENANCE.md` and `NOTICE.md` for retrieval date, what was included, and attribution.
- Rust crate: `publish = false`; this is an evaluation port inside the graphrag examples tree.
- References: https://github.com/jawah/charset_normalizer

## Remaining handoff caveats (P2/P3)

- Ambiguous single-byte/adversarial cases can produce ranking differences; the two pre-existing pinned unstable cases remain expected xfails with source-backed reasons. The new harness reports any future ambiguous divergence as a finding instead of accepting it by category.
- Default test surface includes exhaustive CD and single-byte codec probes, representative multibyte probes, and the 79-input seeded live detector corpus; the 530-input full corpus is opt-in. This is not an unbounded random fuzzer or exhaustive multibyte variant table. Single-byte codecs are exact via generated tables; most multibyte paths use encoding_rs (with documented rare table/extension differences for big5*/euc_jis_2004/iso2022_jp* variants versus Python stdlib codecs).
- No claim of "full upstream parity" or complete feature match. Readiness is scoped to the golden contract, fixed and seeded differential matrices, and product CLI surface on the exercised inputs.
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
