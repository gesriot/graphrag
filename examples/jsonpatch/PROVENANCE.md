# Provenance — vendored `jsonpatch` + `jsonpointer` (Phase 7 ablation v2 target)

Chosen as the v2 capability-ablation target: a fresh, less-familiar, multi-module
Python component (`jsonpatch` depends on `jsonpointer`) with an RFC-defined,
deterministically-testable bounded API. Status: **mini-gate in progress; the
ablation is blocked on a closure-coverage finding (below).**

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
- `byog_jsonpatch`: 104 entities (16 classes, 71 methods, 9 fns, 4 data, 2 files,
  2 modules), 100 calls / 102 contains / 7 uses_data; `audit_call_edges`
  pass_rate 1.0, 0 anomalies/dangling/suspicions.

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
