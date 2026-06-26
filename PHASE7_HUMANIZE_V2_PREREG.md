# Phase 7 Ablation v2 — pre-registration (humanize number-formatting slice)

**Written before running** (only the slice + criteria are fixed here; the full
mini-gate + N=3 ablation run on a fresh weekly budget). Purpose: lock the target,
scope, golden criteria, adequacy criteria, and go/no-go *before* seeing results,
so v2 cannot be retrofit to a flattering outcome. Exact entity titles marked
"(verify at mini-gate)" are confirmed against the vendored source when indexing.

## Why humanize, why this slice
v1 (sqlparse.split) gave an efficiency signal on a familiar target. jsonpatch was
retired as a **boundary case**: its apply-slice is dominated by higher-order /
dynamic-dispatch indirection (`map(self._get_operation)`, registry `cls()`,
polymorphic `operation.apply`) the deterministic call-graph cannot follow without
dataflow/points-to analysis. v2 needs a target that is:
- **multi-module** (real cross-module assembly burden for the raw arm), and
- **statically structured** (plain function calls + module-level data tables +
  cross-module helper calls), so the graph closure can actually be *adequate* —
  i.e. the experiment measures graph-vs-raw, not another closure boundary.

`humanize` is MIT, multi-module (`number.py`, `time.py`, `filesize.py`,
`lists.py`, `i18n.py`, `locale/`), and the **number-formatting core** is exactly
that: pure functions over numbers, backed by data tables and a cross-module i18n
helper, with no polymorphic dispatch.

## Target
- Package: `humanize` (github.com/python-humanize/humanize), **MIT**.
- Vendor `src/humanize/*.py` verbatim (license captured) at mini-gate.

## Scope (bounded slice)
- **API under test:** the `number.py` public formatters, default locale:
  `intcomma`, `intword`, `apnumber`, `ordinal`, `fractional`, `scientific`
  (final set confirmed at mini-gate against the vendored `number.py`).
- **Output contract:** pure `value -> str` (deterministic). Rust API shape:
  `fn intword(value: f64|i64, fmt) -> String` etc. (exact signatures fixed at
  mini-gate from the Python defaults).
- **i18n:** default locale only — the gettext helpers (`_`, `P_`/`ngettext`)
  behave as identity/passthrough; the locale-loading machinery and `locale/`
  catalogs are **out of scope** (treated as passthrough), but the *call into* the
  i18n helper is in scope (it is the cross-module dependency the graph must
  capture and the raw arm must assemble).
- **Out of scope:** `time.py`, `filesize.py`, `lists.py`, locale catalog loading,
  CLI. These are the must-exclude set for adequacy (no overpack into them).

## Golden criteria (gate step 2 — derive from the Python oracle)
- Oracle = vendored `humanize`. For each in-scope function, a corpus pinning
  `input -> exact output string`, derived by running the Python lib.
- Corpus dimensions (per function, ≥ ~30 cases total): zero, negatives, small
  ints, the power/suffix boundaries for `intword` (thousand/million/…/threshold),
  `intcomma` grouping, `ordinal` 1/2/3/11/12/13/21/…, `apnumber` 1–9 vs ≥10,
  `fractional` whole/half/thirds, `scientific` precision, and a few rounding
  edges. No floating-point-printing rabbit hole beyond what the functions emit
  (mirror the v1 lesson: keep numeric edges where the oracle output is stable).
- A Python contract test re-derives from the vendored lib to keep the golden in
  sync; the hidden Rust contract test compares `arm::…` output to the golden.

## Adequacy criteria (gate step 4 — pre-registered, measured by ablation.py adequacy)
Roots = the in-scope `number.py` functions (verify at mini-gate).
- **must-reach:** each in-scope function; the intra-`number.py` helpers they call;
  the module-level data tables they read (e.g. `intword`'s powers/suffix table)
  via `uses_data`; and the cross-module i18n helper(s) `i18n:_` / `i18n:P_`
  (or `ngettext`) — proving the closure crosses `number -> i18n` (verify exact
  titles at mini-gate).
- **must-exclude (no overpack):** any `time:*`, `filesize:*`, `lists:*` function,
  and locale-catalog loaders. The number slice must not drag in the other modules.
- **adequate = all must-reach present AND zero must-exclude leaked.**

## Pre-registered go/no-go
1. Run the mini-gate: vendor/license → scope → golden → index/audit
   (`pass_rate=1.0`, 0 anomalies/dangling/suspicions) → **adequacy**.
2. **Only if adequacy is clean** (must-reach present, must-exclude not leaked)
   do we run N=3. If the closure is *not* adequate, do NOT run N=3: instead fix
   the packer/closure with principled static edges (as in step-1) — and if the
   gap is dynamic indirection like jsonpatch, record humanize's relevant
   sub-slice as another boundary and pick a cleaner slice, rather than forcing it.
3. N=3 per arm, **batched** (graph×3, then raw×3 — never 6 at once), corrected-spec
   discipline: same API spec to both arms, `--no-neighbor-text` + narrow packs (no
   broad class/module/file spans), pre-provided deps if the port needs a crate,
   infra-only invalidation, hidden golden, report all points + medians, no spin.

## Status
Pre-registration committed; jsonpatch fixed as boundary case. Full humanize-v2
(vendor → golden → graph → adequacy → N=3) deferred to a fresh weekly budget.
