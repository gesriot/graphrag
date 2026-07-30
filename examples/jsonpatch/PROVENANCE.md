# Provenance — vendored `jsonpatch` + `jsonpointer` (Phase 7 boundary case)

Initially chosen as the v2 capability-ablation target: a fresh, less-familiar,
multi-module Python component (`jsonpatch` depends on `jsonpointer`) with an
RFC-defined, deterministically-testable bounded API. Status: **retired as the v2
target after the mini-gate exposed a real closure boundary** (details below).

## Source
- `jsonpatch.py` (RFC 6902) and `jsonpointer.py` (RFC 6901), by Stefan Kögl.
- Upstream: github.com/stefankoegl/python-json-patch and python-json-pointer
  (vendored verbatim from `master`).

## License — gate step 1 (captured)
- **Modified BSD (BSD-3-Clause)** for both; full texts in `LICENSE_jsonpatch` and
  `LICENSE_jsonpointer`.

## Scope (bounded)
- API under test: `apply_patch(doc, patch) -> Ok(result) | Err(class)` only.
- Ops: add/remove/replace/move/copy/test; JSON-Pointer escaping (`~0`/`~1`);
  array index and `-`; failed paths; failed `test`; bad pointer; invalid op.
- Error taxonomy (from the Python oracle): `TestFailed`, `Conflict`,
  `InvalidPointer`, `InvalidPatch`.
- Out of scope: mutable in-place API, CLI, `make_patch`/diff, custom pointer
  classes.

## Golden (gate step 2, captured)
- `tests/apply/golden_apply.json`: 25 cases, each `(doc, patch)` ->
  `ok`+`result` or `error` class, derived from the vendored Python library.
- `tests/test_jsonpatch_contract.py` re-derives from the vendored lib to keep the
  golden in sync. (Note: `JsonPatchTestFailed` subclasses `AssertionError`, so the
  test classifies inside the except and asserts outside it.)

## Graph (gate step 3, captured)
- Baseline mini-gate graph: 104 entities (16 classes, 71 methods, 9 fns, 4 data,
  2 files, 2 modules), 100 calls / 102 contains / 7 uses_data;
  `audit_call_edges` pass_rate 1.0, 0 anomalies/dangling/suspicions.
- After graph-frontier step 1, `byog_jsonpatch` remains at 104 entities and has
  222 relationships: 104 calls / 102 contains / 9 property / 7 uses_data, plus
  19 call observations; `audit_call_edges` remains pass_rate 1.0 with 0
  anomalies/dangling/suspicions. (Observations later grew to 31 — see
  *Actionable candidates* below; entities, relationships and pass rate did not
  move.)

## Closure-coverage finding (gate step 4 — BLOCKER for a fair ablation)
The calls-closure from `apply_patch` reaches only **3 entities** and never reaches
`jsonpointer` or the operation classes. Cause: jsonpatch is a
registry+polymorphism architecture, which the deterministic *call* graph
under-captures:
- `JsonPatch.apply` has **zero resolved call edges**; its work is
  `operation.apply(obj)` — a polymorphic call demoted to a weak observation.
- Operations are dispatched through a **static registry**
  (`operations = MappingProxyType({'add': AddOperation, ...})`, jsonpatch.py:508).
  The registry is statically visible, but there is no edge from that data entity
  to the classes it names, so the closure cannot follow it.
- The cross-module dependency `from jsonpointer import JsonPointer, ...` is not
  modeled as an edge, so the closure cannot cross into `jsonpointer`.

Implication: a calls-only context pack would starve the graph arm unfairly (it
would lack the operations and the entire jsonpointer dependency that the raw arm
has). Running the ablation in this state would measure the closure's gap, not
graph-vs-raw — the same class of confound v1's spec bug taught us to avoid.

This is also a real, honest result about the approach: the deterministic call
graph captures static call structure well (sqlparse) but **under-captures
dynamic-dispatch / static-registry / cross-module architectures**. Closing it
requires modeling, at minimum, import edges and static data->entity references
(the registry), then expanding classes to their methods in the closure.

**Diagnostic (2026-07-26):** `scripts/python_dynamic.py` now labels these
blind spots as provenance (`dynamic_dependent` / `dynamic_reasons`) without
demoting `is_deterministic` or adding/dropping edges. On the published
`byog_jsonpatch` graph it flags:
- `jsonpatch:JsonPatch._get_operation` — `registry_lookup:operations`,
  `call_through_dynamic_name:cls<…>`
- `jsonpatch:JsonPatch.apply` — `registry_derived_iter:_ops`,
  `polymorphic_call:operation.apply<…>`
- the weak observation `JsonPatch.apply -> operation.apply`

Context packs surface a top-level `dynamic_warning` (same shape as the C
`preprocessor_warning`). Detection only — not resolution of the missing edges.

**Actionable candidates (same day):** reading the flag alone only told an agent
that *some* implementations exist. The diagnostic now also emits weak
`call_observations` with `reason=registry_candidate:operations['…']` naming
each static table member (`AddOperation.apply`, `RemoveOperation.apply`, …) at
confidence 0.35 — never as `calls` edges. Context packs expose them as
`dynamic.dispatch_candidates` so a porter can open the right methods without
resolver changes or pass-rate moves. On the published graph this is 12 new
observations (19 → 31): six `.apply` targets for `JsonPatch.apply` and the six
operation classes for `JsonPatch._get_operation`. Recall is deliberately narrow
— only dict literals whose values are plain names resolve, so lambda-valued
tables elsewhere (`isodate:STRF_DT_MAP`, `humanize:_TRANSLATIONS`) yield no
candidates rather than guesses.

**Runtime oracle (2026-07-26; corrected 2026-07-28; closed-loop 2026-07-28):**
`scripts/python_dynamic.py --vs-runtime` imports each registry in a
**subprocess** and compares keys/target names to the extracted table. Import
failure is a named skip (not empty-registry agreement). Independent runtime
discovery enumerates callable mappings the AST never named, so omissions are
visible rather than silent. Non-callable tables the AST still flags are
**false-positive detections**, not missed dispatch targets. Rates over an empty
scored population are `n/a`, not 100%.

| package | registry | runtime | extracted | agree | disagree | missed | classification |
|---|---|---:|---:|---:|---:|---:|---|
| `jsonpatch` | `JsonPatch.operations` | 6 | 6 | 6 | 0 | 0 | Name values, fully resolved |
| `semantic_version` | `BaseSpec.SYNTAXES` | 2 | 2 | **2** | 0 | 0 | **decorator registration** (`@BaseSpec.register_syntax`) |
| `isodate` | `STRF_DT_MAP`, `STRF_D_MAP` | 25 | 0 | 0 | 0 | **25** | Lambda values — left missed |
| `humanize` | `_TRANSLATIONS` | 1 | — | — | — | — | not a callable registry |
| `semantic_version` | `SpecItem.KIND_ALIASES` | 2 | — | — | — | — | not a callable registry |
| `charset_normalizer` | `UNICODE_RANGES_COMBINED` | 347 | — | — | — | — | not a callable registry |

**Scored coverage on callable registries: 8/8 agree (jsonpatch 6 + SYNTAXES 2),
0 disagreements; genuine residual miss = isodate's 25 lambdas.**

Decorator registration is statically tractable: a classmethod that does
`cls.REG[key] = subclass`, used as `@Owner.register_*` on a class whose body
sets a string key, emits the same `registry_candidate:` observations as a dict
literal (e.g. `BaseSpec.parse` → `SimpleSpec` / `NpmSpec`). The oracle confirms
those entries against the imported object — a planted wrong candidate is a
disagreement. Independent discovery no longer lists `BaseSpec.SYNTAXES` as
undetected.

**Lambda values (isodate) stay missed on purpose.** Keys are visible, but a
lambda has no honest callee name: many bodies call methods on the bound object
or helpers like `tz_isoformat`, sometimes more than one. Inventing a target
from "the first Call in the body" would be a guess dressed as provenance.
Leaving 25 missed is the correct residual.

False-positive history: an earlier measurement reported "375 missed"; 350 of
those came from non-callable tables (`range`, `NullTranslations`, `str→str`).
The probe already recorded value `kind` — runtime settles what is a registry.

**Call-graph oracle (2026-07-28):** `scripts/call_graph_oracle.py` is the first
measurement of whether published `calls` edges are *observed*, not merely
structurally clean. It loads edges from the **published** snapshot, runs the
package golden contract in a subprocess under `sys.setprofile`, and reports
three disjoint counts (no single agreement numerator).

**Before cross-module import resolution** (oracle baseline):

| package | graph rows / unique | observed | confirmed | missed | unconfirmed |
|---|---:|---:|---:|---:|---:|
| `jsonpatch` (25 apply goldens) | 104 / 84 | 33 | 8 | 25 | 76 |
| `mini_lang` | 69 / 52 | 36 | 32 | 4 | 20 |
| `humanize` (59 cases) | 80 / 42 | 9 | 9 | 0 | 33 |

Of jsonpatch's 25 misses, eleven were cross-module `jsonpatch:* → jsonpointer:*`
— a static `from jsonpointer import JsonPointer` binding the graph did not
follow (PROVENANCE has named this modelling gap since the closure-coverage
finding).

**After** resolving imported names through parameter defaults + `self` attrs
(`pointer_cls=JsonPointer` → `self.pointer_cls(...)` → `self.pointer.to_last`):

| package | graph rows / unique | observed | confirmed | missed | unconfirmed | notes |
|---|---:|---:|---:|---:|---:|---|
| `jsonpatch` | 122 / 94 | 33 | 21 | 12 | 73 | confirmed +13, missed −13 |
| `mini_lang` | 100 / 57 | 36 | 35 | 1 | 22 | small graph densified |
| `humanize` | 124 / 42 | 9 | 9 | 0 | 33 | unique pairs unchanged |

Cross-module residual after that step: **1** (`MoveOperation.apply →
JsonPointer.to_last`, if/else `from_ptr` — left unresolved). The other eleven
cross-module edges were trace-confirmed.

**Registry-dispatch promotion (2026-07-30):** the six
`JsonPatch.apply → *Operation.apply` targets (and six
`_get_operation → *Operation` constructs) are **statically named** Name values
in `JsonPatch.operations`, and the dispatch sites are labelled. Runtime
`--vs-runtime` already agreed 6/6 on that table — that *justifies* promotion;
extract time still requires the static Name binding so lambda/runtime-only
members can never become edges.

| rule field | value |
|---|---|
| When | static registry entry (Name/Attribute value + concrete key) **and** labelled dispatch site on the same class **and** target entity exists |
| confidence | **0.75** |
| `is_deterministic` | **False** (which member runs is runtime) |
| extractor | `python_dynamic_registry` (idempotent replace on re-stamp) |
| Never | lambda/Call-valued tables; runtime-only discoveries; missing entity titles |

Oracle **after promotion** (published graph, 25 apply goldens):

| | confirmed | missed | unconfirmed | calls rows |
|---|---:|---:|---:|---:|
| post-import | 21 | 12 | 73 | 122 |
| **post-registry promote** | **27** | **6** | 79 | **134** |

The six `.apply` edges moved missed → confirmed. Remaining six misses:
`apply → _ops` (property edge, not `calls`), `_ops → _get_operation` (map of
method), `{Copy,Move}.apply → PatchOperation` / `_get_operation → PatchOperation`
(superclass construction), and the if/else `from_ptr → to_last`.

Adequacy (`jsonpatch_adequacy.json` from `apply_patch`, calls+property):

| | reached | must_reach missing |
|---|---:|---:|
| before promotion | 8 | 29 / 37 |
| **after** | **31** | **7 / 37** |

Still missing: `PatchOperation` (+ `__init__` / `apply` / `path` / `key`) as a
superclass not linked by `calls`, and `jsonpointer:JsonPointer.__init__` /
`unescape`. Operation `.apply` methods and jsonpointer `to_last`/`walk`/`get_part`
are now on the closure. `must_exclude` still clean.

`audit_call_edges` on the reindexed jsonpatch graph: pass_rate **1.0**, 0
anomalies, 0 dangling, **134** calls. Other packages were **not** reindexed for
this step (sqlparse pin `20260625-154143-8ce62d57` retained).

A deleted published edge shows as missed; a fabricated edge shows as
unconfirmed — verified end to end against a copied graph on disk, not only
against the scoring function. Tracing overhead is negligible on these packages
(<2s).

Each run reports the corpus it actually executed (`workload cases executed`),
because the first version did not. `run_humanize_number` looked for the golden
at `tests/golden_number.json` while it lives at
`tests/number/golden_number.json`, so it silently substituted an eight-call
hand-written smoke set; its case loop also read `fn`/`function`/`name` while the
golden uses `func`, so a found golden would still have executed zero cases. The
figures above are from the real 59-case golden, and a missing corpus is now a
named skip rather than a substitution.

## Graph-frontier step-1 outcome (tractable edges added; boundary confirmed)
Per the agreed plan, the tractable/static-fact resolver edges were added and each
was measured against `scripts/ablation_specs/jsonpatch_adequacy.json`:

- **1a chained-ctor** (`Cls(args).method()` -> `Cls.method`): correctly captured
  operation delegation (`MoveOperation.apply` -> `Remove/AddOperation.apply`,
  `CopyOperation.apply` -> `AddOperation.apply`).
- **link 1 same-file ctor + factory classmethod resolution** with a collapse
  guard: `apply_patch`'s `patch = JsonPatch(...)` / `JsonPatch.from_string(...)`
  both normalize to `JsonPatch`, so `patch.apply(...)` resolves -> closure reaches
  `JsonPatch.apply`.
- **link 2 property bridge** (`self.<prop>` read of an `@property`): closure
  reaches `JsonPatch._ops`.

These are general resolver wins (they help any Python graph), all with audit
pass_rate 1.0 and the full suite green. But the apply-slice closure then **stalls
exactly at `_ops -> _get_operation`**, which is `tuple(map(self._get_operation,
self.patch))` — a method passed by value. Closure size from `apply_patch` is 5;
it never reaches the operation registry, the operation classes, or `jsonpointer`.

**Boundary conclusion (pre-registered go/no-go):** the remaining links are genuine
higher-order / dynamic-dispatch / points-to problems, out of scope for the current
deterministic resolver without dataflow analysis:
- `map(self._get_operation, …)` — callable passed by value;
- `cls = self.operations[op]; cls(op)` — dynamic instantiation via a registry value;
- `operation.apply(obj)` — polymorphic dispatch;
- `self.pointer.to_last(…)` — cross-module self-attribute type propagation.

So `jsonpatch` is a **boundary case** for the deterministic graph, not a fair
capability-ablation target (its graph arm would be honestly starved by the
indirection, not by a packer gap). The capability v2 moves to a more statically
structured target (`humanize`); `jsonpatch` stands as a documented frontier:
the call-graph captures static structure well but under-captures dynamic-dispatch
architectures.
