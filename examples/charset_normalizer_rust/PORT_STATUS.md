# charset_normalizer_rust — Port Readiness / Productization Status

Date: 2026-07-26

This records a scoped readiness pass for packaging, docs, CLI parity, and handoff artifacts.
It does not change the detector algorithm.

Reference Python: `examples/charset_normalizer/` (with PROVENANCE.md, LICENSE, NOTICE.md).
Rust port: `examples/charset_normalizer_rust/`
Saved GraphRAG context packs: `examples/charset_normalizer_rust/packs/`

## What this port targets (for evaluation/handoff)

Core + product surface:
- `from_bytes` and options-aware / trace variants
- `from_path` / `from_reader` / `from_fp` (+ `_with_options*`)
- typed `is_binary` byte, reader, and path forms (+ options/trace variants)
- `FromBytesOptions`
- `detect_legacy` / `detect_chardet_compatible`
- `CharsetMatch` / `CharsetMatches` observation surface used by CLI + normalization
- `VERSION_STRING` / `VERSION` Rust equivalents of the vendored Python version metadata
- Full upstream-style CLI (`normalizer`) with normalize/JSON/minimal/verbose/alternatives/threshold/preemptive/replace/force + stdin support

## Achieved on captured contracts (scoped language)

- **Golden contract (byte-exact)**: 18/18 samples in `tests/golden_detection.json` (captured from Python reference *before* any Rust code; see `tests/detection_contract.rs`).
- **Off-golden best-match assertions**: exact encoding matches on additional fixed cases.
- **Fixed/adversarial differential matrix**: 72 CLI/detector pytest items (17 fixed generated + 21 earlier seeded + 26 adversarial payloads + fixtures/toggles). Current status: 70 pass + 2 expected xfails for adversarial inputs whose best-encoding tie-break is intentionally unstable (short_20 resolved by matching Python `str.isprintable()` on U+00A0 in mess detection).
- **Seeded live differential harness**: Python is executed as the oracle on each run; Rust is observed through its own strict decoder, rather than re-decoding Rust's selected label in Python. The default `check_port.sh` corpus has 79 generated inputs across 15 source-encoding categories (real UTF-8/BOM, UTF-16 LE/BE, Latin-1, CP1251, Shift-JIS, GB2312, truncations, invalid/mixed runs, empty/single/very-short, multi-valid, and seeded mutations). The on-demand full corpus has 530 inputs, adding all 256 one-byte values, 240 seeded mutations, and long text. Seed `20260725`: 79/79 and 530/530 strong agreements, respectively; no divergences were suppressed. The harness initially exposed two port bugs: a tie-order mismatch on the 1,584-byte `real_utf16le` input (Python `Swedish`, Rust `German`) caused by a Rust-only language-order tie-break and hash-map merge, and an over-broad `English` inference for the one-byte `00`/`e3` and five-byte `4100420043` inputs. The port now preserves Python's stable candidate order and infers English only for an actual `ascii` match; both findings are covered by Rust regression tests and the live sweep.
- **Exhaustive codec/CD parity**: 6 pytest items covering all supported single-byte `encoding_languages`, multibyte language mapping, all 0x00..0xFF strict decode probes for single-byte codecs, representative multibyte strict decode probes, and representative encode/output round-trips. Current status: 6 pass, no codec xfails. The corpus checks all 7,445 Python HZ shifted pairs in both strict directions plus replacement-mode U+20AC; all four UTF-7 SIG forms against the running `api.py` path; all 23 formerly excluded UTF output cases; and complete live Python maps for the generated EUC-JIS and Shift-JIS-X-0213 profiles. The only codec behavior deliberately left non-exact is the quantified ISO-2022 extension family below.
- **CLI byte-exact / snapshot**: non-verbose JSON, minimal output, argparse errors, paths (abs), stdin, normalize side-effects (written bytes), replace/force flows.
- **Normalized verbose trace parity**: when `explain=true` (or `--verbose`), trace events from api/md match in content (timestamps, some floats/sets masked for determinism; not raw log strings).
- **API-surface audit**: `API_SURFACE_AUDIT.md` mechanically enumerates the Python root exports, model members, CLI flags, and accessible helper modules. Its live Python-oracle coverage now includes version metadata, `CharsetMatch.byte_order_mark`, typed `is_binary` reader/path forms, `CliDetectionResult` JSON, direct submatches, replacement/default output, and the formerly stubbed utils/CD helpers.

**Total Rust tests**: 83 (58 unit parity in cd/models/md/codec/API + 9 CLI + 3 contract/golden + 13 off-golden/large-lazy). All pass; 0 ignored.

## Byte-exact vs. normalized parity (precise)

- Byte-exact: golden JSON, off-golden best assertions, CLI non-verbose output + normalize output bytes, test matrix best-encoding on stable cases.
- Normalized parity (traces): key detection events (e.g. "passed initial chaos probing", "definitive match", fallback, fast-track, skip reasons) with structural/fuzzy matching suitable for explain-mode debugging.
- Do not expect byte-identical verbose logs or floating point text.

### Differential agreement policy

The seeded harness compares the canonical best encoding, strictly decoded Unicode text, language, BOM flag, and chaos/coherence scores (each within 0.005) on live Python and Rust observations. A Python result is *confident* only when the payload is at least 32 bytes, its chaos is at most 0.05, and the sorted result has one de-duplicated candidate. A confident difference fails the default `check_port.sh` gate. Tiny, noisy, or multi-candidate cases use the same comparison but are printed as explicit ambiguous findings, with the payload and decoded-text fingerprints needed to reproduce them; they are never silently counted as agreement. The current two historical detector xfails are still retained in the fixed adversarial matrix and are not covered up by this policy.

## API-surface scope (specific)

`API_SURFACE_AUDIT.md` is the authoritative row-by-row matrix. In brief:

- **Covered:** detection with `FromBytesOptions`; byte/reader/path input forms; version metadata through `VERSION_STRING`/`VERSION`; `CharsetMatch` observations, direct submatches, replacement/default output; `CharsetMatches` access/iteration/append; `CliDetectionResult`; callable utils/CD helpers; legacy migration wrappers; and every vendored Python CLI flag.
- **Deliberately excluded:** global `set_logging_handler` state (use trace collection or `--verbose`); making Rust's established `detect(&[u8])` mean Python's legacy top-level `detect` (use `detect_legacy` or `detect_chardet_compatible`); and showing Rust-only cp-isolation/exclusion controls in Python-compatible help. These are design choices, not claims that the operations are impossible in Rust.
- **Not applicable:** Python `__str__`, `__repr__`, implicit truthiness, dynamic union dispatch, subclass/plugin extension protocols, and the argparse callback/process-global `cli_detect` helpers. Rust exposes typed methods, `Option`, iteration, concrete detector components, and the observable `normalizer` binary instead.

The legacy post-processing (small-sample confidence adjustment, `utf_8_sig` mapping, and `CHARDET_CORRESPONDENCE`) is implemented only in `detect_legacy*`. The detection codec contract matches Python `api.py` (SIG/BOM stripping per `should_strip_sig_or_bom` except UTF-16/32; UTF-7 full-decode-then-strip). Raw `codecs.decode("utf_7")` retains U+FEFF, so it is the wrong oracle for a recognized SIG: Rust decodes the complete `+/v8`, `+/v9`, `+/v+`, or `+/v/` stream and removes exactly its first U+FEFF. Its canonical UTF-7 output now matches Python's complete 128-byte ASCII direct-set/shift-boundary corpus, and UTF-16 output matches Python for all 21 formerly excluded BOM/endian cases. HZ, the three EUC-JIS profiles (`euc_jis_2004`, `euc_jisx0213`, `euc_jp`), and the two Shift-JIS-X-0213 profiles (`shift_jis_2004`, `shift_jisx0213`) are generated from the running Python codecs. The latter contribute 11,296 / 11,271 / 25 and 11,286 / 11,261 / 25 strict decode forms / scalar encodes / canonical sequences, respectively. Rust uses custom handling for UTF, HZ, Korean, and generated JIS tables; the ISO-2022 refusal is the sole named profile boundary.

### Closed codec scope

The port claims exact codec behavior only where the corpus proves it: all
single-byte byte values; HZ's 7,445 shifted pairs; the three generated EUC-JIS
profiles; the two generated Shift-JIS-X-0213 profiles; UTF-7 SIG and ASCII
output boundaries; UTF-16 output; and the existing representative/default
detection contracts. The remaining ten named ISO-2022 cases are a deliberate,
quantified refusal, not a test omission. The port has one `encoding_rs`
ISO-2022-JP profile with 7,392 non-ASCII scalar encodes. The reproducible
isolated-scalar encode enumeration reports the following Python domains and
differences from that profile:

| Python codec | Scalars | Python-only | Rust-only | Remapped |
| --- | ---: | ---: | ---: | ---: |
| `iso2022_jp_1` | 12,947 | 5,791 | 236 | 280 |
| `iso2022_jp_2` | 18,600 | 11,365 | 157 | 359 |
| `iso2022_jp_2004` | 11,207 | 3,970 | 155 | 363 |
| `iso2022_jp_3` | 11,197 | 3,960 | 155 | 363 |
| `iso2022_jp_ext` | 13,010 | 5,791 | 173 | 343 |

Reproduce the table (the probe emits `E\t<scalar-hex>\t<bytes-hex>` per encodable
scalar; the Python side is the stdlib codec):

```bash
cargo run --quiet --bin parity_probe -- encoding-rs-scalar-map iso-2022-jp > /tmp/rs_iso.tsv
uv run python - <<'PY'
rs = {int(s, 16): bytes.fromhex(b) for _, s, b in
      (l.rstrip("\n").split("\t") for l in open("/tmp/rs_iso.tsv"))}
for enc in ("iso2022_jp_1", "iso2022_jp_2", "iso2022_jp_2004", "iso2022_jp_3", "iso2022_jp_ext"):
    py = {}
    for s in range(0x80, 0x110000):
        if 0xD800 <= s <= 0xDFFF:
            continue
        try:
            py[s] = chr(s).encode(enc, "strict")
        except UnicodeEncodeError:
            pass
    shared = set(py) & set(rs)
    print(enc, len(py), len(set(py) - set(rs)), len(set(rs) - set(py)),
          sum(1 for s in shared if py[s] != rs[s]))
PY
```

The ten asserted cases are `iso2022_jp_1` French/Greek, `iso2022_jp_2`
French/Greek/Chinese/Korean, `iso2022_jp_2004` French, `iso2022_jp_3` French,
and `iso2022_jp_ext` French/Greek. For each, Python emits a stateful escape
sequence while the current Rust profile rejects both strict decode and encode.
Closing that boundary would require five separate generated stateful ISO-2022
encoders/decoders, rather than a table overlay. That is the sixth codec frontier
and is deliberately not started. Finite corpus coverage is also a policy
boundary: no claim is made about arbitrary untested byte strings.

## Test commands (current counts as of this status)

From inside Rust dir:
```bash
cargo fmt
cargo test --quiet   # expects 83 passing tests (see breakdown above)
```

From repo root:
```bash
PYTHONPATH=. uv run pytest examples -q --tb=no
# expected current summary: 886 passed, 2 xfailed
# xfails are the documented adversarial detector cases (bom8_badcont, short_high)
# (short_20 xfail burned down via narrow is_printable fix matching Python source)
# MB codec note: exact generated maps cover HZ, EUC-JIS, and Shift-JIS-X-0213; the five quantified ISO-2022 extension profiles are the only named non-exact codec boundary.
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
- Default test surface includes exhaustive CD and single-byte codec probes, every valid generated Python HZ shifted pair, all strict forms of the three generated EUC-JIS and two Shift-JIS-X-0213 profiles, the complete 23-case UTF output boundary, the ten asserted ISO-2022 refusal cases, representative other-multibyte probes, and the 79-input seeded live detector corpus; the 530-input full corpus is opt-in. Finite test corpora are a deliberate verification policy, not a claim about arbitrary untested byte strings. The five quantified ISO-2022 extension profiles are the only named codec behavior the port does not claim exact.
- API availability is itemized in `API_SURFACE_AUDIT.md`: the prior “simply not done” surface is now closed, and the remaining intentional differences and Python-only mechanics are explicitly named.
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
