# Provenance – vendored `humanize` (Phase 7 ablation v2 target)

Capability-ablation v2 target (see `PHASE7_HUMANIZE_V2_PREREG.md`): a multi-module,
statically-structured Python slice (number formatters backed by data tables and a
cross-module i18n helper), chosen deliberately so the graph closure can be made
adequate – unlike the `jsonpatch` boundary case (dynamic dispatch).

## Source
- `humanize` (github.com/python-humanize/humanize), vendored from `master`:
  `__init__.py`, `number.py`, `i18n.py`, `filesize.py`, `time.py`, `lists.py`.
- `_version.py` is a **vendored-snapshot shim** (`0+vendored-snapshot`); upstream
  `_version.py` is build-generated (setuptools_scm) and not in the source tree.
  It only satisfies `__init__`'s `from ._version import __version__`.

## License – gate step 1 (captured)
- **MIT** (`LICENSE`, vendored verbatim from upstream `LICENCE`).

## Common evidence status

`uv run python scripts/port_eval.py --gate humanize` deliberately reports a
**GAP**: this source contract has no Rust port directory or Rust golden
consumer. It is listed in `examples/PORT_EVIDENCE.md` rather than being treated
as a passing port.

## Scope (bounded slice – gate step 2, frozen)
- `number.py` formatters, default locale: `intcomma(value, ndigits=None)`,
  `intword(value, format="%.1f")`, `apnumber(value)`, `ordinal(value, gender="male")`,
  `fractional(value)`, `scientific(value, precision=2)`.
- Helpers: `_format_not_finite`. Data tables: `powers`, `human_powers`,
  `_ORDINAL_SUFFIXES`, `_APNUMBER_WORDS`, `_SUPERSCRIPT_MAP`, `_SUPERSCRIPT_TRANS`.
- Cross-module i18n (aliased imports): `_` = `i18n:_gettext`, `P_` = `i18n:_pgettext`,
  `NS_` = `i18n:_ngettext_noop`, plus `_ngettext`, `decimal_separator`,
  `thousands_separator`. Default locale = passthrough; locale-catalog loading and
  `time/filesize/lists` are out of scope.

## Golden (gate step 2, captured)
- `tests/number/golden_number.json`: 59 cases derived from the Python oracle
  (intword power boundaries, intcomma grouping incl. negative/ndigits, ordinal
  teens 11/12/13 + 21/22/23, apnumber <10 / >=10, fractional, scientific incl.
  precision variants). `tests/test_humanize_number_contract.py` re-derives to keep
  the golden in sync.

## Graph (gate step 3, captured)
- `byog_humanize`: `audit_call_edges` pass_rate 1.0, 0 anomalies/dangling/
  semantic suspicions; 56 calls / 58 contains / 25 uses_data.

## Adequacy (gate step 4/6 – STANDALONE RESULT: not clean, two tractable boundaries)
Closure from the 6 number roots (size 20) **correctly** reaches the data tables
(`powers`/`human_powers`/`_ORDINAL_SUFFIXES`/`_APNUMBER_WORDS`/`_SUPERSCRIPT_TRANS`),
non-aliased i18n (`_ngettext`/`decimal_separator`/`thousands_separator`), and leaks
**zero** `time/filesize/lists` (no overpack). It misses exactly 4 must-reach nodes,
from two precise, *tractable static* boundaries (not dynamic dispatch):

1. **Aliased cross-module imports.** The slice calls `_(…)`/`P_(…)`/`NS_(…)`, which
   are `from .i18n import _gettext as _, _pgettext as P_, _ngettext_noop as NS_`.
   The resolver tracks the call to the alias but does not map the alias back to the
   imported symbol, so `i18n:_gettext`, `i18n:_pgettext`, `i18n:_ngettext_noop` are
   unreached. (Non-aliased `_ngettext` resolves fine – confirming the gap is the
   alias, not cross-module imports per se.)
2. **Data->data reference.** `_SUPERSCRIPT_TRANS = str.maketrans(_SUPERSCRIPT_MAP)`
   reads another data entity, but there is no edge from a data assignment's RHS to
   the data entity it references, so `_SUPERSCRIPT_MAP` is unreached.

Per the pre-registered go/no-go, N=3 is **not** run through this dirty closure.
Both boundaries are principled static facts (aliased import resolution; data->data
reference edges), fixed in the resolver before re-measuring – distinct from the
jsonpatch boundary, which was genuinely dynamic (higher-order/points-to).

### Resolution (both boundaries fixed; adequacy now clean)
`scripts/extract_python.py` gained two general resolver edges (commit after the
mini-gate):
- **aliased-import resolution** (`import_orig`): a call to `_`/`P_`/`NS_` resolves
  to `i18n:_gettext`/`_pgettext`/`_ngettext_noop`;
- **data->reference edges**: a module-level data table's RHS that names other
  entities emits `references` edges (`human_powers` -> `i18n:_ngettext_noop`,
  `_SUPERSCRIPT_TRANS` -> `_SUPERSCRIPT_MAP`).

Re-measured: **adequacy clean** – closure size 24, all 19 must-reach present, 0
must-exclude leaked; `byog_humanize` audit pass_rate 1.0 (calls 56->80 are
previously-dropped aliased/imported calls that now resolve), full suite green.
Both fixes are general wins (any data-heavy Python graph, e.g. charset-normalizer).

### N=3 outcome (see PHASE7_ABLATION.md for the full table)
Ran N=3 per arm after a clean dry-prep audit. Result: **near-parity, no capability
gap** – arm_graph median 59/59, arm_raw median 58/59; the only recurring miss is
the shared f64 `intword(1e100)` googol edge. Raw agents found the slice
"well-contained" (`number.py`+`i18n.py` among 6 modules), so raw-assembly was easy
and the graph showed no advantage. humanize's number slice is a weak capability
discriminator; the capability claim remains undemonstrated (needs a target with
high raw-assembly cost that is still adequacy-clean).
