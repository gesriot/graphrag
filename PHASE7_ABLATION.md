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
path patched), and a per-case scorer reports partial pass-rate.

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

## Reproduce

```bash
# graph arm kit (transitive closure) + raw arm kit (whole package)
uv run python scripts/ablation.py prep --target sqlparse_split --graph byog_sqlparse \
  --source examples/sqlparse \
  --closure-root lexer:tokenize \
  --closure-root engine.statement_splitter:StatementSplitter.process \
  --closure-root engine.filter_stack:FilterStack.run \
  --api scripts/ablation_specs/sqlparse_split_api.md --out /tmp/ablation/sqlparse
# (fill each kit with a cold sub-agent, then:)
uv run python scripts/ablation.py eval --kit /tmp/ablation/sqlparse/arm_graph \
  --golden-dir examples/sqlparse/tests/split \
  --contract-test examples/sqlparse_rust/tests/split_contract.rs --crate-name sqlparse_rust
```

## Next

1. **Done:** corrected-protocol existing-benchmark ablation (above) — efficiency
   win, no capability win on a familiar target.
2. Optional packer follow-up: also pull conditionally wired pipeline elements
   (e.g. `StripTrailingSemicolonFilter`) into the closure, which would likely
   close the residual graph gap on the two `strip_semicolon` cases.
3. **v2 (the real capability test):** a fresh, larger, less-familiar multi-file
   target with no strong model prior, same N=3 protocol and metrics. This is where
   the graph's "find and assemble the right slice" value should — or should not —
   show up as a pass-rate gap, not just efficiency.
