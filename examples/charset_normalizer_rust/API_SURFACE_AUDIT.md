# charset-normalizer Python → Rust API-surface audit

Date: 2026-07-25  
Python reference: `examples/charset_normalizer/` (`__version__ == 3.4.7`)  
Rust crate: `examples/charset_normalizer_rust/`

This is an API inventory, not a claim that identically named entry points have
identical language-level calling conventions. The public package boundary is
the Python package's mechanical `__all__`; its values are re-exported from
`charset_normalizer.__init__`. The audit also enumerates the two public model
types and the command-line flags, as requested. Non-root implementation modules
are addressed separately so their accessible helpers are not silently treated as
covered API.

## How this was enumerated

The inventory was derived from the vendored source, rather than recalled:

```bash
rg -n '^def |^class ' examples/charset_normalizer/{__init__,api,legacy,models,version,utils,cd,md}.py
rg -n '^def |^class ' examples/charset_normalizer/cli/__main__.py
rg -n '^pub (fn|struct|enum|const|type)' examples/charset_normalizer_rust/src/{lib,models,utils,cd,md}.rs
```

The category has a deliberately narrow meaning:

- **Covered** – Rust exposes the behavior, sometimes in the idiomatic typed
  shape shown in the status column.
- **Deliberately out of scope** – an intentional difference recorded here and
  in `PORT_STATUS.md`, with its reason.
- **Not applicable** – Python object-model or runtime-dispatch mechanics that
  have no direct Rust spelling. This does not disguise a missing behavioral API.
- **Simply not done** – a real capability absent from the Rust public surface or
  behavior. These are not implied exclusions.

## Package-root exports (`charset_normalizer.__all__`)

| Python surface | Rust status | Category |
| --- | --- | --- |
| `from_bytes(sequences, steps, chunk_size, threshold, cp_isolation, cp_exclusion, preemptive_behaviour, explain, language_threshold, enable_fallback)` | `from_bytes`, `from_bytes_with_options`, and trace variant; `FromBytesOptions` carries the keyword controls. | Covered (typed adaptation) |
| `from_fp(fp, ...)` | `from_fp` plus `_with_options` and `_with_options_and_trace`. | Covered (typed adaptation) |
| `from_path(path, ...)` | `from_path` plus `_with_options` and `_with_options_and_trace`. | Covered (typed adaptation) |
| `is_binary(fp_or_path_or_payload, ...)` | Slice: `is_binary`/`is_binary_bytes`; reader: `is_binary_reader`; path: `is_binary_path`; all forms now have options, and reader/path have trace variants. | Covered (typed adaptation) |
| `detect(byte_str, should_rename_legacy=False, **kwargs)` | `detect_legacy(byte_str, should_rename_legacy)` returns `LegacyDetectionResult`; `detect_chardet_compatible` is the default-Python spelling. Rust `detect` deliberately remains the pre-existing modern-best-match shortcut. | Deliberately out of scope for same-name behavior – documented migration entry points avoid a breaking change to the established Rust `detect`. |
| `CharsetMatch` | Rust `CharsetMatch`. Member-by-member audit below. | Covered in part – see below. |
| `CharsetMatches` | Rust `CharsetMatches`. Member-by-member audit below. | Covered in part – see below. |
| `__version__` | `VERSION_STRING == "3.4.7"`. Rust identifiers cannot start with `__`. Oracle-backed by `test_api_surface_parity.py`. | Covered (Rust naming adaptation) |
| `VERSION` (`["3", "4", "7"]`) | `VERSION == ["3", "4", "7"]`. Oracle-backed by `test_api_surface_parity.py`. | Covered |
| `set_logging_handler(...)` | No global logger mutation. Use `from_*_with_options_and_trace(..., explain=true)` or CLI `--verbose`. | Deliberately out of scope – global Python logging state is not part of the typed Rust API. |

## `CharsetMatch`

| Python surface | Rust status | Category |
| --- | --- | --- |
| constructor `CharsetMatch(payload, guessed_encoding, mean_mess_ratio, has_sig_or_bom, languages, decoded_payload, preemptive_declaration)` | Public Rust struct fields support construction, but no Python-shaped constructor/lazy cache arguments. | Not applicable – Rust construction and ownership are explicit. |
| `multi_byte_usage` | `multi_byte_usage() -> Option<f64>`. | Covered (method/property adaptation) |
| `encoding` | Public `encoding: String`. | Covered |
| `encoding_aliases` | `encoding_aliases() -> Vec<String>`. | Covered |
| `bom` | Public `bom: bool`. | Covered |
| `byte_order_mark` | `byte_order_mark() -> bool`, an alias for `bom`; Python-oracle checked. | Covered |
| `languages` | `languages() -> Vec<String>`. | Covered |
| `language` | Public `language: Option<String>`; absence is represented as `None`, rather than Python's derived `"Unknown"`. | Covered (typed adaptation) |
| `chaos` | Public `chaos: f64`. | Covered |
| `coherence` | Public `coherence: f64`. | Covered |
| `percent_chaos` | `percent_chaos() -> f64`. | Covered |
| `percent_coherence` | `percent_coherence() -> f64`. | Covered |
| `raw` | Public `raw: Vec<u8>`. | Covered |
| `submatch` | `submatch() -> &[CharsetMatch]`. | Covered |
| `has_submatch` | `has_submatch() -> bool`. | Covered |
| `alphabets` | `alphabets() -> Vec<String>`. | Covered |
| `could_be_from_charset` | `could_be_from_charset() -> Vec<String>`. | Covered |
| `fingerprint` | `fingerprint() -> Option<u64>`. Hash width and process seeding are Rust-specific. | Covered (typed adaptation) |
| `output(encoding="utf_8")` | `output(encoding)` follows Python replacement-mode encoding; `output_default()` is Rust's explicit zero-argument equivalent and `output_strict()` preserves the fallible codec operation. Oracle cases cover ASCII/CP1252, Shift-JIS, Johab, ISO-2022-KR, the UTF-8 default, HZ, all UTF output modes, and generated EUC-/Shift-JIS maps. `tools/generate_codecs.py` derives HZ's 7,445-pair strict GB2312 map, three EUC-JIS maps, and two Shift-JIS-X-0213 maps from the running Python codecs; codec/CD parity builds those expectations live. | Covered (typed adaptation). |
| `add_submatch(other)` | `add_submatch(other) -> Result<(), AddSubmatchError>` appends a distinct owned match and rejects same encoding + decoded-fingerprint matches. | Covered (typed adaptation); Python's non-`CharsetMatch` runtime-type rejection is not applicable in a statically typed call. |
| `__str__`, `__repr__` | `decoded()` returns the decoded string; no `Display`/`Debug` parity contract. | Not applicable – Python lazy string/repr semantics are object-model behavior. |
| `__eq__`, `__lt__` | Rust derives structural `PartialEq`; sorting is internal to `CharsetMatches`. Python's string comparison and ranking protocol are not exposed. | Not applicable – Python operator protocol and cross-type equality have no direct typed equivalent. |

## `CharsetMatches`

| Python surface | Rust status | Category |
| --- | --- | --- |
| constructor `CharsetMatches(results=None)` | `CharsetMatches::new(Option<Vec<CharsetMatch>>)`. | Covered (typed adaptation) |
| `__iter__` | `iter()` and `IntoIterator for &CharsetMatches`. | Covered |
| `__getitem__(int)` | `get(index) -> Option<&CharsetMatch>`. | Covered (fallible access adaptation) |
| `__getitem__(str)` | `get_by_encoding(encoding) -> Option<&CharsetMatch>`. | Covered (fallible access adaptation) |
| `__len__` | `len()`. | Covered |
| `__bool__` | `is_empty()`; callers negate it. | Not applicable – Rust has no implicit truthiness. |
| `append(item)` | `append(item)` preserves ordering and submatch factoring. | Covered |
| `best()` | `best() -> Option<&CharsetMatch>`. | Covered |
| `first()` | `first() -> Option<&CharsetMatch>`. | Covered |

`CliDetectionResult` is public from `charset_normalizer.models` but not a
package-root export. Rust now exposes `CliDetectionResult` with the same fields,
constructor order, and `to_json()` layout (`ensure_ascii=True`, four-space
indentation). The live oracle test compares an accented path, an astral Unicode
path, arrays, booleans, and floating-point fields byte-for-byte.

## CLI flags

The Python CLI parser in `cli/__main__.py` is the source for this table. All
shared flags are present in `normalizer`; existing snapshot tests cover help,
errors, JSON, stdin, and normalize/replace behavior.

| Python CLI surface | Rust status | Category |
| --- | --- | --- |
| positional `files` (including `-`) | files and stdin supported. | Covered |
| `-v`, `--verbose` | supported. | Covered |
| `-a`, `--with-alternative` | supported. | Covered |
| `-n`, `--normalize` | supported. | Covered |
| `-m`, `--minimal` | supported. | Covered |
| `-r`, `--replace` | supported. | Covered |
| `-f`, `--force` | supported. | Covered |
| `-i`, `--no-preemptive` | supported. | Covered |
| `-t`, `--threshold` | supported. | Covered |
| `--version` | supported. | Covered |
| Rust-only hidden `--cp-isolation`, `--cp-exclusion` | extra test/harness controls, intentionally omitted from shared help text. | Deliberately out of scope – additive Rust extension, not a claimed Python flag. |
| `cli.__main__.query_yes_no`, `FileType`, `cli_detect(argv)` | The `normalizer` binary implements the observable prompt, file validation, argument parsing, and exit-status behavior; existing CLI snapshots exercise it. Rust does not expose Python's argparse callback or a process-global `argv` function as a library API. | Not applicable – these are Python CLI-framework entry/callback mechanics, not an independently usable package API. |

## Non-root helper modules: audit boundary and outstanding work

Python does not declare `__all__` for `utils`, `cd`, or `md`, and none of these
names are re-exported by the package root. They are nevertheless importable, so
this audit records their status rather than implying they are full-parity API.

| Python non-root surface | Rust status | Category |
| --- | --- | --- |
| `cd.encoding_languages`, `cd.mb_encoding_languages`, `cd.alphabet_languages`, `cd.characters_popularity_compare`, `cd.alpha_unicode_split`, `cd.merge_coherence_ratios`, `cd.coherence_ratio` | Same-named `pub` functions, covered by the exhaustive CD oracle tests where applicable. | Covered (module-local helper surface) |
| `cd.encoding_unicode_range`, `cd.unicode_range_languages`, `cd.get_target_features`, `cd.filter_alt_coherence_matches` | Same-named public Rust functions; oracle checks cover Cyrillic/Latin ranges, French target features, and em-dash alternative filtering. | Covered (module-local helper surface) |
| `md.unicode_range`, `md.remove_accent`, `md.is_suspiciously_successive_range`, `md.mess_ratio` | Same-named public Rust functions. | Covered (module-local helper surface) |
| `md.CharInfo` and the `MessDetectorPlugin`/nine concrete plugin classes | Rust has concrete detector implementation structs but no Python subclass/override extension protocol. | Not applicable – Python inheritance/plugin extension semantics have no direct Rust equivalent. |
| `utils.is_accentuated`, `remove_accent`, `unicode_range`, `is_latin`, `is_punctuation`, `is_symbol`, `is_emoticon`, `is_separator`, `is_case_variable`, `is_cjk`, `is_hiragana`, `is_katakana`, `is_hangul`, `is_thai`, `is_arabic`, `is_arabic_isolated_form`, `is_cjk_uncommon`, `is_unicode_range_secondary`, `is_unprintable` | Concrete public Rust helpers replace the former stubs. `remove_accent` returns `Result<char, RemoveAccentError>` because the Python reference can raise on compatibility decompositions. Oracle characters cover Latin, punctuation/symbols, CJK scripts, Arabic forms, controls, U+001A, U+FEFF, and the Python error case U+FEFB. | Covered (typed adaptation) |
| `utils.any_specified_encoding`, `is_multi_byte_encoding`, `identify_sig_or_bom`, `should_strip_sig_or_bom`, `iana_name`, `cp_similarity`, `is_cp_similar`, `cut_sequence_chunks` | Concrete public Rust helpers now use `Option`, `Result`, and materialized `Vec<String>` rather than Python runtime dispatch/generator protocol. Oracle checks cover alias failure modes, multibyte classification, UTF BOMs, declared encodings, code-page similarity, and chunks. | Covered (typed adaptation) |
| `utils.set_logging_handler` | See package-root row. | Deliberately out of scope |

## What this change closed, and what it does not claim

This audit has now closed the former “simply not done” list. In addition to the
earlier version/BOM/input-form work, the Rust surface supplies
`CliDetectionResult`, direct owned submatches, Python-style replacement output
with an explicit Rust default method, and the formerly stubbed `utils`/missing
`cd` helpers. `tests/test_api_surface_parity.py` executes the vendored Python
reference and the Rust `parity_probe` for every group.

Codec scope is deliberately narrower than API availability. UTF-7 signature
decoding is covered against `api.py`: for each of `+/v8`, `+/v9`, `+/v+`, and
`+/v/`, the complete stream is decoded and exactly one leading U+FEFF is
removed. The old raw-stdlib comparison was therefore a wrong oracle, not a
Rust exclusion. `euc_jis_2004` is now exact through a reproducible Python-codec
map (17,363 valid non-ASCII decode forms, 14,429 scalar encodes, and 25
canonical two-scalar entries); the same generator also closes the distinct
`euc_jisx0213` (17,353 / 14,419 / 25) and `euc_jp` (13,009 / 13,010 / 0)
profiles discovered while unmasking the prior xfail. The direct pre-fix
`encoding_rs::EUC_JP` enumeration differed from Python `euc_jis_2004` by
7,078 Python-only / 41 legacy-only / 341 remapped encode entries and 3,907 /
10 / 381 decode entries, respectively. This is not a general
claim of exhaustive multibyte parity. The former 23 UTF output-mode cases now
match Python exactly, including all 21 UTF-16 BOM/endian cases and the two UTF-7
cases, and the two former Shift-JIS-X-0213 profile cases now use generated
Python maps: `shift_jis_2004` has 11,296 decode forms / 11,271 scalar encodes /
25 canonical sequences and `shift_jisx0213` has 11,286 / 11,261 / 25. The only
named codec boundary is the quantified ISO-2022 refusal in `PORT_STATUS.md`:
five stateful Python profiles differ from the single `encoding_rs` ISO-2022-JP
profile by 3,960–11,365 Python-only scalar encodes, plus 155–236 Rust-only and
280–363 remapped entries per profile. The ten explicitly asserted representative
cases reject in Rust rather than being silently skipped. Implementing five
generated stateful codecs is deliberately left as the next, unstarted frontier.

The only reclassification is the Python CLI-framework trio
`query_yes_no`/`FileType`/`cli_detect`: it is **not applicable** as a Rust
library API because the `normalizer` binary already covers the observable CLI
contract, while an argparse callback and process-global argv entry point have no
independent typed consumer meaning. The port still deliberately excludes global
logger mutation and same-name legacy `detect` behavior, and does not claim
unbounded fuzzing or exhaustive multibyte codec-variant parity.
