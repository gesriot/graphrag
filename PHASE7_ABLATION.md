# Phase 7 Ablation v1 — does the deterministic graph help a cold porting agent?

**Date:** 2026-06-25. First ablation toward the Phase 7 question: the project has
shown *we can drive ports through graph rails*, but not yet *causally* that the
graph part gives a measurable advantage over raw source. This is **v1 on an
existing benchmark** (`sqlparse.split`); a fresh-target replication (v2) is the
planned follow-up to remove the "you picked a familiar base" objection.

## Method

Two arms per target, each a self-contained Cargo *kit* filled in by a **cold
sub-agent** (fresh context, no project history), via `scripts/ablation.py`:

- **arm_graph** — only the graph-derived context packs (transitive callee closure
  from the entry symbols: entities + call edges + code snippets + observations).
- **arm_raw** — only the raw original source (the whole package, tests excluded).

Both arms get the *same* required public API spec (the interface the hidden golden
needs) and the *same* fixed prompt with an allowed-path rule (read only inside the
kit). Neither kit contains the golden corpus or the reference Rust port. After the
agents finish, `ablation.py eval` scores each kit against the **hidden** golden in
a throwaway copy (the reference contract test is injected with crate name + golden
path patched). For this v1 benchmark, a target-specific per-case scorer was used
to report partial pass-rate when a run failed the aggregate contract.

Honest scope: kits share a filesystem, so this is an engineering ablation (prompt
rule + transcript audit), not a sealed lab. A fully blind run would need separate
sandboxes.

## Dry-run (jsmn) — harness validation

| arm | builds | golden | compile attempts | tool-uses | wall |
|---|---|---|---|---|---|
| arm_graph | ✓ | pass | 1 | 13 | 86s |
| arm_raw | ✓ | pass | 1 | 9 | 75s |

Purpose was to debug the protocol, and it did: the graph arm had to **infer
`jsmn_fill_token`** because a hand-picked symbol list under-packed the graph. Fix
applied: the graph arm now packs the **transitive callee closure**, not a manual
list. Also confirmed the obvious limitation — a single-file target is uninformative
(raw = the whole implementation in one header), so the real run uses a multi-file
component.

## v1 result (`sqlparse.split`, 25 hidden golden cases)

| arm | material | builds | split golden | tool-uses | wall |
|---|---|---|---|---|---|
| **arm_graph** | 11 closure packs (no keyword tables) | ✓ (1st try) | **24/25** | 17 | 164s |
| **arm_raw** | whole 21-file package incl. `keywords.py` | ✓ | **25/25** | 21 | 373s |

### Reading the result (straight, not spun)

- **Both arms reproduce `split` cold at high fidelity.** A capable LLM ports this
  component from either material; the component is "LLM-portable" regardless.
- **The graph arm reached near-parity (24/25) with ~half the material** (11 focused
  packs vs a 21-file package) and fewer tool-uses / less wall time. On this target
  the graph's value shows up as **focus/efficiency**, not a raw capability gain.
- **The single-case gap is a fixable packer limitation, not a graph weakness.**
  The closure packs carry call-reachable *functions* but not *module-level data*:
  the `KEYWORDS_*` dicts and `SQL_REGEX` live only in `keywords.py`, which arm_raw
  retained and arm_graph had to reconstruct. The one case arm_graph missed is
  keyword-dependent. Concrete improvement: `context_pack` should also include the
  data-table dependencies a symbol reads, not only the functions it calls.
- **This target does not cleanly isolate the graph's value.** arm_graph rebuilt a
  plausible keyword set from its own prior and still hit 24/25, i.e. the LLM's
  training prior substitutes for missing context on a well-known library. A
  less-familiar fresh target (v2) is needed to separate "graph helped" from "model
  already knew sqlparse."

## Honest caveats

- `arm_raw`'s self-report was truncated by an account session limit at the very end
  of its run; its kit nonetheless builds clean and is scored objectively (25/25),
  so the eval is valid even though its self-narrated "compile attempts / what was
  hard" is missing.
- v1 is a known benchmark with an existing reference port in the repo (used only as
  the hidden oracle, never shown to the arms). The persuasive claim requires the v2
  fresh-target replication.
- Sub-agents are cold but share training priors; the independent variable is only
  graph-context vs raw-source.

## What v1 establishes / does not

Establishes: the harness works end-to-end on a real multi-file component; the
graph's focused closure gets a cold agent to near-parity with far less context;
and a concrete packer gap (data-table dependencies) to close.

Does **not** establish: that the graph beats raw source in pass-rate (raw won
25 vs 24 here, because it carried more data). The efficiency signal is real; the
capability signal needs v2 on a fresh, larger target where raw-source assembly is
genuinely costly and the model has no strong prior.

## Post-v1 review fix

The v1 graph arm was under-packed in two related places, both fixed after the
measurement without rewriting the historical result:

- Python extraction now models module-level assignments as `data` entities and
  emits `uses_data` edges for functions/methods that read module constants or
  imported module constants. For `sqlparse`, this surfaces `keywords:SQL_REGEX`
  and all `keywords:KEYWORDS_*` tables.
- `context_pack` now emits first-class `data_dependencies`, separate from the
  generic `text_units` slice, so large tables cannot silently fall out of the
  first-N snippets.
- The call closure also catches chained same-class `self/cls` method calls such
  as `cls._default_instance.default_initialization()`, which makes the split
  closure include `Lexer.default_initialization`, `set_SQL_REGEX`,
  `add_keywords`, and their data dependencies.

The corrected `sqlparse.split` graph pack has 15 closure packs and includes the
10 keyword/regex data tables in `Lexer.default_initialization`'s pack.

## Corrected-v1 rerun — invalidated by a spec bug (honest)

Re-running arm_graph on the corrected packs scored **7/25** — far worse than the
prior 24/25. Diagnosing the surprise (rather than reporting it) showed it was **a
bug in the ablation's API spec, not a real signal**:

- The new agent confirmed the packs now carried `SQL_REGEX` + all 9 keyword dicts
  verbatim (the data gap is genuinely closed).
- But every failure was the same systematic whitespace divergence (`"select 1; "`
  vs golden `"select 1;"`). The spec said *"preserve the original text/whitespace"*,
  whereas `sqlparse.split` is `[str(stmt).strip() for stmt in stack.run(...)]` —
  it **strips each statement**. The corrected agent obeyed the (wrong) spec; the
  earlier agent had ignored it and stripped, which is the only reason it scored
  24/25.

Consequences:
- The corrected-v1 number (7/25) is **void** — it measures spec-compliance, not
  graph-vs-raw.
- The **original v1 was also partly confounded**: arm_raw saw the real strip in
  `__init__.py:split` while arm_graph (no pack for the top-level wrapper) had to
  guess; it guessed right, but that is luck, not signal.

Fixes applied: the API spec now states the strip/semicolon contract correctly as
the *definition* of the public API (given equally to both arms). A second lesson:
a single cold run is high-variance — the two arm_graph runs chose very different
internal strategies (lean tokenizer vs hand-rolled `SQL_REGEX` with no regex
crate), so a credible result needs several runs per arm and/or removing avoidable
variance (e.g. pre-providing a `regex`/`fancy-regex` dependency so agents do not
hand-roll regex engines).

Both prior numbers are retired as confounded. The experiment was then re-run under
the corrected protocol below.

## Corrected-v1 result (N=3/arm, corrected protocol)

Protocol (pre-registered, not changed mid-flight): corrected API spec (strip
contract stated as the API definition, given to both arms); `fancy-regex`
pre-provided to both kits (so the variable is graph-vs-raw, not "did the agent
hand-roll a regex engine"); 3 cold sub-agents per arm; identical hidden golden (25
split cases); a run is voided only for infrastructure failure (session drop / kit
not written / dep unresolved), never for a weak agent strategy. All three raw runs
were re-run once after a session-limit interruption (infra invalidation); the
graph runs completed first time.

| arm | run scores | median | min–max | build attempts | tool-uses | wall (s) |
|---|---|---|---|---|---|---|
| **arm_graph** (15 focused closure packs) | 25, 23, 23 | **23/25** | 23–25 | 1,1,3 | 22–31 | 341–369 |
| **arm_raw** (whole 21-file package) | 25, 25, 25 | **25/25** | 25–25 | 1,1,2 | 32–49 | 400–408 |

### Reading the result (straight)

- **Raw is consistently perfect (25/25 ×3); graph is near-parity but lower and more
  variable (median 23/25, range 23–25).** On this familiar benchmark the graph does
  **not** beat raw on fidelity.
- **The graph arm reaches near-parity with ~half the material** (15 focused packs
  vs a 21-file package), fewer tool-uses (median ~29 vs ~33), and ~15% less wall
  time. The measurable win is **efficiency/focus**, not pass-rate.
- **The residual graph gap is small, traceable, and points to a concrete packer
  gap.** Both graph failures are the same `strip_semicolon=true` detail: graph
  stripped only the *last* statement's `;` where sqlparse strips *per statement*.
  The `StripTrailingSemicolonFilter` body is not in the closure (it is wired
  conditionally inside `FilterStack` and was not reached from the 3 roots), so the
  graph arm implemented strip from the spec wording — graph_1 got it right, graph_2
  and graph_3 did not. Raw had the filter source and all three were exact. So the
  gap is partly a still-missing closure element (the filter) and partly within-arm
  variance, not a fundamental graph weakness.

### Honest conclusion (existing-benchmark)

On a component the model already knows well, with the data/keyword gap closed and
regex variance removed, **raw source ≥ graph on fidelity (25 vs median 23), and the
graph's value is efficiency** (much less context, fewer tools, less time) at a
small, traceable fidelity cost. This is an efficiency result, not a capability
result. The capability claim — that the graph lets an agent succeed where
raw-source assembly is genuinely costly — is still **not** demonstrated and needs
v2 on a fresh, larger, less-familiar target. Two concrete packer follow-ups also
fell out of v1: include module-level data dependencies (done) and conditionally
wired pipeline elements like filters (open).

## Post-v1 infrastructure validation: charset-normalizer

After the `sqlparse.split` data-dependency gap was fixed, `charset-normalizer`
was added as a heavier production-library stress-test for the same mechanism. It
is not an ablation result, but it validates that the corrected graph/context-pack
rails can carry large module-level tables and heuristic detector logic into a
scoped Rust port:

- `api:from_bytes`, `md:mess_ratio`, `cd:*`, `models:*`, and `utils:*` packs were
  generated with first-class `data_dependencies`.
- The saved packs live with the Rust port at
  `examples/charset_normalizer_rust/packs/` instead of a new repository-root
  `packs/` convention.
- The local graph artifact `byog_charset_normalizer` is intentionally ignored by
  Git like other BYOG snapshots, but can be kept locally to rerun `context_pack`
  and graph queries without reindexing.
- The scoped port currently passes its handoff gates (`check_port.sh`; 81 Rust
  tests; full examples pytest expected `444 passed, 4 xfailed` after adding
  humanize-v2's Python contract tests).

This strengthens confidence that the `uses_data` / `data_dependencies` fix is
useful beyond `sqlparse`, while leaving the actual graph-vs-raw capability claim
to the pre-registered `humanize` v2 experiment.

## Reproduce

```bash
# graph arm kit (transitive closure) + raw arm kit (whole package)
uv run python scripts/ablation.py prep --target sqlparse_split --graph byog_sqlparse \
  --source examples/sqlparse \
  --closure-root lexer:tokenize \
  --closure-root engine.statement_splitter:StatementSplitter.process \
  --closure-root engine.filter_stack:FilterStack.run \
  --dep 'fancy-regex = "0.13"' \
  --api scripts/ablation_specs/sqlparse_split_api.md --out /tmp/ablation/sqlparse
# (fill each kit with a cold sub-agent, then:)
uv run python scripts/ablation.py eval --kit /tmp/ablation/sqlparse/arm_graph \
  --golden-dir examples/sqlparse/tests/split \
  --contract-test examples/sqlparse_rust/tests/split_contract.rs --crate-name sqlparse_rust
```

## v2 result (humanize number slice, N=3 per arm)

Target `humanize.number` (default locale), 6 formatters, 59-case hidden golden
from the Python oracle. Pre-registered in `PHASE7_HUMANIZE_V2_PREREG.md`; run only
after the adequacy gate was clean (closure 24, 19/19 must-reach, 0 must-exclude
leaked) and a manual dry-prep audit. The mini-gate itself was a real result: it
surfaced two *tractable* closure boundaries, fixed as **general resolver wins** —
aliased-import resolution (`_`/`P_`/`NS_` -> `i18n:_gettext`/`_pgettext`/
`_ngettext_noop`) and data->reference edges (`human_powers` -> `i18n:_ngettext_noop`,
`_SUPERSCRIPT_TRANS` -> `_SUPERSCRIPT_MAP`). All fixes kept every existing graph
audit at pass 1.0 and the full suite green (442 passed, 4 xfailed; 444 after
the isodate v3 contract tests).

| arm | scores | median | build attempts | tool-uses | wall (s) |
|---|---|---|---|---|---|
| **arm_graph** (24 closure packs) | 59, 58, 59 | **59/59** | 2,1,1 | 22–30 (med 25) | 163–235 (med 177) |
| **arm_raw** (whole 6-module package) | 58, 58, 59 | **58/59** | 1,1,1 | 13–20 (med 19) | 148–227 (med 187) |

### Reading the result (straight)

- **Near-parity, no capability gap.** Both arms reproduce the slice at very high
  fidelity (graph median 59/59, raw median 58/59). The graph does not win on
  pass-rate here.
- **The one recurring miss is a shared f64 edge, not a graph-vs-raw signal.**
  `intword(1e100)` ("1.0 googol") failed in 3 of 6 runs (graph_2, raw_1, raw_2)
  and passed in the other 3, across *both* arms: the frozen `f64` value type cannot
  hold Python's bignum `10**100`, so the googol threshold is stochastic. It affects
  both arms equally.
- **No efficiency win either — honestly, the opposite.** Raw used slightly *fewer*
  tools (median 19 vs 25). Every raw agent reported the slice was "well-contained"
  (`number.py` + `i18n.py`, obvious among only 6 modules), so the raw-assembly
  burden was low and the graph's focus advantage (seen in v1's 21-module
  `sqlparse`) did not manifest. **humanize's number slice is a weak capability
  discriminator** — easy enough for raw that the graph adds no measurable edge.

### Honest conclusion across v1 / jsonpatch / v2

The capability claim — that the deterministic graph lets a cold agent succeed
where raw-source assembly is genuinely costly — remains **undemonstrated**:
- v1 (`sqlparse.split`, familiar, 21 modules): efficiency signal only.
- `jsonpatch`: documented dynamic-dispatch boundary (call graph under-captures).
- v2 (`humanize.number`, fresh, but ~2 relevant modules): near-parity, no
  advantage, because raw assembly was easy.

What *is* solidly demonstrated: the adequacy-gated methodology is sound and drove
real, general resolver/packer improvements (data-dependency packing from
`sqlparse`; aliased-import + data-reference edges from `humanize`; closure-scoped,
leak-audited graph-arm material). The missing ingredient for a capability win is a
target that is simultaneously (a) genuinely high raw-assembly cost — a slice buried
across many interdependent modules, not one obvious file — and (b) statically
structured enough to be adequacy-clean (not a jsonpatch-style dynamic boundary).

## v3 result (isodate `parse_duration`, N=3 per arm)

Pre-registered in `PHASE7_ISODATE_V3_PREREG.md` and run only after the mini-gate
was clean (parser-only closure 16, 13/13 must-reach, 0 must-exclude leaked; graph
audit pass 1.0, 0/0/0) and the dry-prep material audit passed. Target chosen to
satisfy the ingredient v1/v2 lacked: `parse_duration`'s implementation is spread
across **8 interdependent modules**, so the raw arm must locate and assemble the
slice out of a 10-file package, while the graph arm receives 13 closure packs.

Arms were filled by **GPT-5.6 (Terra, High)** — a different model family from
v1/v2's Claude sub-agents, recorded in the pre-registration before any run. Both
arms used the same model at the same setting; only the material differed. Scores
are harness-measured against the hidden 24-case golden; efficiency columns are
agent self-reports and are **not** comparable to v1/v2's (different harness).

| arm | scores | median | min–max | build attempts (self-reported) | tool-uses (self-reported) |
|---|---|---|---|---|---|
| **arm_graph** (13 closure packs) | 24, 23, 24 | **24/24** | 23–24 | 3,3,2 | 8–13 (med 13) |
| **arm_raw** (whole 10-file package) | 24, 24, 24 | **24/24** | 24–24 | 2,3,4 | 8–18 (med 15) |

Isolation evidence (`verify-fill`) is clean on all six: only `src/lib.rs`
modified, nothing written outside `src/`, zero foreign-material flags. Run
artifacts and evidence are archived in `examples/isodate/ablation_v3/`.

### Reading the result (straight)

- **No capability win, again.** Both arms reproduce the slice at median 24/24.
  Raw was perfect in all three runs; graph missed one case in one run. On the
  target designed specifically to make raw assembly expensive, raw was not merely
  competitive — it was marginally more consistent.
- **No efficiency win either.** Graph's median tool-uses (13) versus raw's (15)
  is well inside the noise of three runs of self-reported "roughly N", and build
  attempts (3,3,2 vs 2,3,4) show no direction. v1's clear focus advantage did not
  reappear.
- **The single graph miss is within-arm variance, not missing material.** Graph
  run 2 failed `P0003-06-04T12:30:05`, the alternative datetime form reached
  through the deepest part of the closure. The packs for `parse_datetime`,
  `parse_date` and `parse_time` were all present in that kit — the other two
  graph runs got the case right from the same material. So it is not a packer
  gap of the kind v1 surfaced.
- **The design premise held; the prediction did not.** Raw genuinely had to
  assemble across 10 files, and it did so in 8–18 tool calls without difficulty.
  High raw-assembly cost, at this scale, simply is not a barrier for a capable
  model.
- **Confound worth naming:** the prereg claimed a weak model prior. That is true
  of *isodate the library*, whose specific quirks the golden pins (`PT` → zero
  timedelta, `P` → error, the alternative form), but ISO 8601 durations are a
  published standard the model knows well. The prior was weaker than for
  `sqlparse` but not absent.

### Honest conclusion across v1 / jsonpatch / v2 / v3

Four attempts, three of them adequacy-gated and pre-registered, one across a
second model family: **the capability claim is not demonstrated, and the
accumulated evidence now argues against it for this class of target.** On
bounded, statically structured, single-entry-point library slices, a capable
model ports as well from raw source as from a graph closure. The graph's
measurable value in this series has been focus/efficiency on the largest target
(v1's 21-module `sqlparse`) and nothing on the smaller ones.

The structural reason is now visible, and it is a property of the experiment, not
of the graph: **any slice small enough to be a clean benchmark is also small
enough for the raw package to fit in the model's context.** Every target tried is
under ~4k LOC with one obvious entry point, so "raw-assembly cost" was only ever
locating code, never being unable to see it. The condition under which a code
graph should matter — the condition the original demo implies at 1M LOC — is when
raw *cannot* be handed over at all and must be triaged.

That reframes what a v4 would have to be, if one is run: a target whose raw
material genuinely exceeds the arm's context budget, so `arm_raw` must choose
what to read while `arm_graph` receives exactly the closure. Until such a design
exists, the honest project claim is the one the evidence supports — the
deterministic graph is a verification and context-assembly discipline that makes
ports auditable and repeatable, not a demonstrated accuracy multiplier.

What remains solidly demonstrated is the methodology: adequacy gating and the
material audits drove real, general improvements (data-dependency packing from
`sqlparse`; aliased-import and data-reference edges from `humanize`;
constructor/operator edges from `isodate`), and each gate caught real defects
before they could contaminate a published number.

## Next

1. **Decide whether a v4 is worth it** on the reframed premise above (raw exceeds
   context, not merely spread across files). If not, stop the series and record
   the negative result as the finding — four pre-registered attempts is enough to
   report honestly rather than keep searching for a favourable target.
2. Optional: widen the golden value type beyond `f64` (int/float/bignum) for a
   future numeric target — the cause of the shared `intword(1e100)` miss, not a
   porting failure.
