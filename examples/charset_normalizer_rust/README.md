# charset_normalizer_rust

Rust port of the core of `charset-normalizer` (jawah/charset_normalizer).

This is an experiment using the graphrag-code deterministic extraction + context packs pipeline.

## Gates status (as of 2026-06-27)
- ✅ License: MIT (vendored + recorded in examples/charset_normalizer/PROVENANCE.md)
- ✅ Golden contract captured **before** any Rust: 18 cases from real sample files. Python reference matches 18/18.
  - `examples/charset_normalizer/tests/golden_detection.json`
  - Also copied to `tests/golden_detection.json` here.
- ✅ Graph indexed (`byog_charset_normalizer`)
- ✅ `audit_call_edges`: structural pass rate **1.0**, 0 anomalies, 0 dangling, 0 semantic suspicions.
- ✅ Context packs generated for key symbols (e.g. `api:from_bytes` pulls data tables via `uses_data` / data_dependencies). Saved packs are checked in under `packs/` beside this Rust crate; the local `byog_charset_normalizer` snapshot is intentionally ignored like other reproducible graph artifacts.
- ✅ Rust core port implemented with real candidate probing, mess detection, coherence/language scoring, BOM/SIG handling, and deterministic sorting.
- ✅ File/reader API layer: `from_bytes`, `from_bytes_with_options`, `from_reader`, `from_fp`, `from_path`, and typed `is_binary` byte/reader/path forms with options/trace variants.
- ✅ Legacy chardet-style wrapper: `detect_legacy(byte_str, should_rename_legacy)`, `detect_chardet_compatible(byte_str)` (defaults to rename=false for chardet names), `LegacyDetectionResult`; includes upstream `CHARDET_CORRESPONDENCE` and small-sample/BOM post-processing. Note: top-level `detect(&[u8]) -> Option<CharsetMatch>` is preserved as the simple modern best-match path (different from Python's `detect` which is the legacy entrypoint).
- ✅ `CharsetMatch` surface slice:
  - `decoded()`
  - replacement-mode `output(target_encoding)`, explicit `output_default()`, and `output_strict(target_encoding)` for codec probes
  - preemptive declaration patching in `output()` for `encoding` / `charset` / `coding` headers
  - `alphabets()` and `byte_order_mark()`
  - `languages()`, `percent_chaos()`, `percent_coherence()`, `multi_byte_usage()`, `fingerprint()`
  - full Python-order `encoding_aliases()` table from `encodings.aliases`
  - `add_submatch()`, `submatch()`, `has_submatch()`, `could_be_from_charset()`
  - `CharsetMatches::append()` factors identical decoded payloads into submatches
  - `CharsetMatches::first()`, `len()`, `is_empty()`, `iter()`, indexed lookup, alias lookup, and borrowed iteration.
- ✅ Python-compatible codec backend for the upstream `IANA_SUPPORTED` set:
  - exact generated Python charmap tables for 66 single-byte codecs (DOS/OEM, EBCDIC, Mac, KOI8, ISO-8859, `latin_1`, etc.)
  - strict UTF-32 and UTF-7 handlers; UTF-7 SIG detection decodes the full stream then removes exactly one leading U+FEFF, matching `api.py` for all four `+/v` marks
  - exact generated Python HZ/GB2312 shifted-pair table and state machine (strict and replacement mode), plus generated strict maps for `euc_jis_2004`, `euc_jisx0213`, `euc_jp`, `shift_jis_2004`, and `shift_jisx0213`
  - generated `johab` and `iso2022_kr` tables/state machine
  - most other multibyte (big5/cp95x, euc_*, iso2022_jp_*, shift_jis, gb*, etc.) via encoding_rs profiles; the five ISO-2022 extension profiles are the only named non-exact codec boundary, quantified below
- ✅ Product CLI slice:
  - one or more files, or `-`/stdin
  - JSON output with `path`, `encoding`, `language`, `chaos`, `coherence`, `has_sig_or_bom`, `encoding_aliases`, `alternative_encodings`, `alphabets`, `unicode_path`, `is_preferred`
  - `--minimal`, `--with-alternative`, `--threshold`, `--verbose`, `--no-preemptive`, `--version`
  - `--normalize` for stdin and files
  - `--normalize --replace` for in-place UTF-8 rewrite, including interactive confirmation unless `--force` is set
- ✅ Python-vs-Rust CLI snapshot tests cover help/version, argparse-style errors, pretty JSON, absolute paths, stdin, minimal output, normalization side effects, replace/force, and prompt-decline behavior.
- ✅ `cargo test` (83 tests total): 58 unit parity tests (cd/models/md/codec/API) + 9 CLI byte-exact/trace tests + 3 detection-contract tests (including 18/18 golden) + 13 off-golden/large-lazy integration tests pass; 0 ignored.
- ✅ Python-vs-Rust pytest matrix: fixed CLI/detector differential has 72 items (70 pass + 2 expected xfails for ambiguous adversarial inputs); the bounded live seeded differential gate runs 79 Python-oracle cases through `tools/check_port.sh` (79 strong agreements, seed `20260725`); exhaustive codec/CD parity adds 6 passing items, including all 7,445 Python HZ shifted pairs, all four UTF-7 SIG forms against the live API oracle, all 23 UTF output cases, and complete generated EUC-/Shift-JIS-X-0213 maps. API-surface oracle coverage adds 1 passing item. Full `PYTHONPATH=. uv run pytest examples -q --tb=no` is expected to report 714 passed, 2 xfailed. The remaining xfails are only the two ambiguous detector cases; the only named non-exact codec scope is the quantified five-profile ISO-2022 refusal below.

## Scope
Core detection:
- `from_bytes`
- Supporting mess/coherence detection + unicode/encoding tables from `constant.py`

Product/API slice:
- `from_bytes` / `from_bytes_with_options` / `from_bytes_with_options_and_trace`
- `from_reader` / `from_fp` / `from_path` (and their `_with_options` / `_with_options_and_trace` variants)
- `FromBytesOptions` for `steps`, `chunk_size`, thresholds, fallback, preemptive behaviour, cp_isolation/exclusion, explain, language_threshold
- binary checks: `is_binary*` variants
- legacy: `detect_legacy(byte_str, should_rename_legacy)`, `detect_chardet_compatible(byte_str)` (chardet names)
- `detect` (modern best-match only)
- upstream-aligned CLI and normalization flow

Main entry signatures (Rust):

```rust
pub fn from_bytes(sequences: &[u8]) -> CharsetMatches;
pub fn from_bytes_with_options(sequences: &[u8], options: FromBytesOptions) -> CharsetMatches;
pub fn from_bytes_with_options_and_trace(sequences: &[u8], options: FromBytesOptions) -> (CharsetMatches, Vec<String>);

pub fn from_path<P: AsRef<Path>>(path: P) -> io::Result<CharsetMatches>;
pub fn from_reader<R: Read>(reader: R) -> io::Result<CharsetMatches>;
pub fn from_fp<R: Read>(reader: R) -> io::Result<CharsetMatches>;
// ... _with_options and _with_options_and_trace equivalents exist

pub struct FromBytesOptions { pub steps: usize, pub chunk_size: usize, ... /* see lib.rs */ }

pub fn detect_legacy(byte_str: &[u8], should_rename_legacy: bool) -> LegacyDetectionResult;
pub fn detect_chardet_compatible(byte_str: &[u8]) -> LegacyDetectionResult;
```

CLI: binary `normalizer` (see [[bin]] in Cargo.toml). Shared flags mirror Python: -v/--verbose, -a/--with-alternative, -n/--normalize, -m/--minimal, -r/--replace, -f/--force, -i/--no-preemptive, -t/--threshold, --version. Supports files, `-` for stdin, and normalization rewrite (with confirm/force). The Rust binary also accepts `--cp-isolation` / `--cp-exclusion` as Rust-only parity/test harness extensions that map to `FromBytesOptions`; they are intentionally omitted from `--help` so shared help text remains byte-exact with the vendored Python CLI.

## Parity scope (precise)
Golden (byte-exact on 18/18 samples), off-golden (exact best-match assertions), fixed deterministic diff matrix (17 fixed + 21 earlier seeded + 26 adversarial payloads, plus fixtures/toggles), a live seeded Python-oracle differential gate (79 inputs by default, 530 with `--full`), exhaustive CD + single-byte codec probes, representative multibyte codec probes, large/lazy paths, and CLI byte-exact (non-verbose JSON/outputs/side-effects) + normalized trace parity (verbose logs).

Distinctions:
- Byte-exact: golden JSON/CLI non-verbose cases, off-golden best assertions, normalize outputs.
- Normalized verbose trace parity (ts/floats/sets masked; events from api/md; not raw logs).
- Expected xfails: only 2 adversarial detector cases with unstable best-encoding tie-breaks (bom8_badcont, short_high). UTF-7 SIG behavior is tested against the live `api.py` oracle; HZ, EUC-JIS, and Shift-JIS-X-0213 maps are generated from Python codecs; and all 23 former UTF output exclusions are exact. The sole named codec refusal is five stateful ISO-2022 profiles: relative to the 7,392-scalar `encoding_rs` profile, Python has 3,960–11,365 extra scalar encodes per profile (with 155–236 Rust-only and 280–363 remapped entries); see `PORT_STATUS.md` for the per-profile table. This is not a claim of exhaustive multibyte variant parity.
- Untested in default runs: broad random corpora and exhaustive multibyte variant tables beyond representative probes. 100k+ scale is covered by the opt-in harness (see below).

API availability is itemized in `API_SURFACE_AUDIT.md`: it distinguishes covered typed APIs (including `CliDetectionResult`, direct submatches, replacement/default output, and callable helper modules), deliberate exclusions (global logging and the same-name legacy `detect` behavior), and Python-only mechanics.

## Reproduce the porting rails
1. `uv run python scripts/context_pack.py "api:from_bytes" --graph byog_charset_normalizer --full-text`
2. Use packs + original sources + golden for the implementation.
3. Implement `from_bytes` (and callees in md/cd) until the contract test passes.
4. `cargo test`

Regenerate codec tables (only when intentionally refreshing Python codec data):
`python3 tools/generate_codecs.py && cargo fmt`

CLI smoke:
`cargo run --bin normalizer -- tests/data/sample-french-1.txt`
`cargo run --bin normalizer -- --minimal tests/data/sample-french-1.txt`
`cargo run --bin normalizer -- --normalize tests/data/sample-french-1.txt`

Run graph queries:
`uv run python scripts/graph_query.py ... --graph byog_charset_normalizer`

## Scale/performance confidence (opt-in, not in default CI)
To exercise 100k+ LOC source-like + large UTF-8 / Western / CJK+Arabic+Cyrillic / HTML-XML-decl / binary-ish payloads and compare Python vs Rust (best encoding, runtimes, size, match):

  CN_SCALE=1 PYTHONPATH=. uv run python examples/charset_normalizer_rust/tools/scale_harness.py

- Uses release build of the Rust CLI for realistic timings.
- Deterministic generators (fixed seeds).
- Explicitly excluded from default runs (`cargo test`, `PYTHONPATH=. uv run pytest examples`).
- Produces a concise table + summary.

## Handoff / repeatable verification
One-command surface (from repo root):

  examples/charset_normalizer_rust/tools/check_port.sh

- runs: cargo fmt --check, cargo test --quiet, targeted pytest for the rust port tests
- `--full`: also run full `PYTHONPATH=. uv run pytest examples -q --tb=no`
- `--scale`: also run the opt-in scale harness (not default)
- prints the xfail policy
- exits non-zero on unexpected (real) failures

See `tools/check_port.sh` and PORT_STATUS.md for exact expectations.

## Golden contract (must match exactly)
See `tests/golden_detection.json` and the test that loads it.

The contract pins for each sample input file:
- detected encoding
- language
- chaos (within epsilon)
- coherence
- bom flag

## Vendored Python side
See `../charset_normalizer/` (with its own PROVENANCE.md).

## References
- Original: https://github.com/jawah/charset_normalizer
- This experiment follows the per-project gate from the graphrag-code Plan.md.
