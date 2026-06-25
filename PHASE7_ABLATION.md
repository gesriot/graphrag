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

**Status: no valid existing-benchmark ablation number yet.** Both prior numbers are
retired as confounded. The harness and packer are now correct; the experiment must
be re-run under the corrected spec with a variance-aware protocol.

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

1. Re-run the existing-benchmark ablation under the **corrected API spec** (strip
   contract fixed), with a **variance-aware protocol**: N runs per arm (both arms,
   since the spec changed) and a pre-provided regex dependency to remove the
   hand-rolled-regex confound. Report distributions, not single numbers.
2. Only then v2 replication on a fresh, larger multi-file target with no strong
   model prior, same metrics, to test the capability (not just efficiency) claim.
