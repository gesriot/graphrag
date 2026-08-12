# Evidence audit – current claims (2026-07-28)

This audit reviews the present-tense capability language in
[`PHASE7_WRITEUP.md`](../PHASE7_WRITEUP.md) and [`Plan.md`](../Plan.md) against
the repository's runnable evidence. It does not rewrite dated historical
records. Where an older record needs qualification, the two entry documents add
a dated superseding statement instead.

## Findings and edits

| Document claim | Evidence status | Edit made |
| --- | --- | --- |
| A deterministic graph makes an LLM more capable than raw source. | Unsupported and contradicted for the tested small-slice regime. The closed ablation record reports no capability or efficiency win across its valid runs. | State the negative result as the current boundary and remove superiority as a success condition. |
| The graph is generally needed because raw/vector retrieval cannot provide coherent code understanding. | No repository experiment establishes that broad causal claim. | Recast this as a design hypothesis or future comparison, not a demonstrated advantage. |
| `Plan.md` calls a hybrid graph/LLM arrangement “the winning pattern.” | The repo has not compared that arrangement against all alternatives or measured a win from it. | Change “winning” to “proposed.” |
| The bounded ports are successful. | Supported within each declared contract by source-oracle/golden tests, fresh graph audit, Rust tests, and `port_eval`; this does not prove full-library parity or a causal graph advantage. | Scope “high-fidelity” and “repeatable” to the declared contracts. |
| C preprocessor liveness labels, cJSON's ownership boundary, registry discovery, and the aggregate gate are merely implementation detail. | Underclaimed. Each has a runnable adversarial check: compiler-preprocessor comparison, header corruption/refusal-span checks, runtime import oracle with planted disagreement, and fail-closed manifest coverage. | Add a dated evidence-boundary update naming the check and its scope. |
| The cJSON aliasing refusal says no Rust representation can implement the C operation. | Unsupported. The checked compiler candidates prove the safe-borrow boundary of the current exclusive-ownership tree only. | Keep the representation/cost wording and explicitly reject an “impossible in Rust” reading. |
| Earlier current frontier text invites more charset-normalizer hardening or further small-slice ablations. | Stale. The charset scope and cJSON exclusive-ownership scope are closed; the ablation series is closed with its negative result. | Make new work require a separately scoped target or protocol rather than incremental reopening. |
| `PHASE7_WRITEUP.md` generalized its raw-context explanation from all tested slices to any clean benchmark slice. | The archive supports the observed target class, not a universal statement about repository size or context budget. | Limit the explanation to the series and name the untested larger-budget regime. |
| The v1 material/tool observation was presented as an efficiency advantage. | The corrected-v1 protocol did not establish an efficiency win, and the closed series conclusion rejects that claim. | Preserve the historical row and add a dated correction that states the full negative result. |

## Runnable evidence referenced by the edits

```bash
# fail-closed aggregate portfolio gate
uv run python scripts/port_eval.py --all-gates --full

# cJSON header-derived surface, C-oracle/ASan traces, compiler-span checks, Miri
examples/cjson/tools/check_port.sh --full

# C preprocessor labels against an independent compiler oracle
PYTHONPATH=. uv run pytest examples/cjson/tests/test_c_liveness_vs_compiler.py -q

# Python registry AST extraction against imported runtime objects
PYTHONPATH=. uv run pytest examples/jsonpatch/tests/test_python_dynamic_runtime_oracle.py -q
```

The tests intentionally preserve negative evidence: unknown C regions are not
counted as compiler agreements; lambda-valued registries are reported as missed
rather than guessed; a planted wrong registry candidate must disagree; and the
cJSON compiler proof requires its diagnostic primary span to occur at the C-trace
mutation.

The ablation result remains the record in `PHASE7_ABLATION.md`: the graph is
supported as an auditability, provenance, adequacy-gating, and context-assembly
discipline, not as a measured accuracy multiplier for the benchmark class.

The dated bootstrap, phase, and ablation paragraphs were intentionally left
unchanged. The entry documents carry the superseding 2026-07-28 statements.
