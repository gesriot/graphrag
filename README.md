# graphrag-code

`graphrag-code` is a reproducible research repository for deterministic code
graphs and bounded Python/C → Rust ports. For each declared target it captures a
source-language behavior contract, rebuilds and audits a graph, assembles context
packs, and checks the Rust port against that contract. The goal is auditable
evidence, not a claim that a graph makes a model intrinsically better at coding.

## What this demonstrates — and what it does not

The declared port profiles show that this process can produce repeatable,
high-fidelity results **within explicit target contracts**. The evidence is
golden-first: behavior comes from the source-language oracle, not from a
hand-written Rust expectation. Fresh graph audits, context packs, source
contracts, Rust checks, and `port_eval` make each result inspectable.

The closed, pre-registered graph-vs-raw ablation series found **no measured
accuracy or efficiency advantage** for the graph arm on its tested benchmark
class. The graph is therefore supported here as a discipline for auditability,
provenance, adequacy gates, and focused context assembly — **not** as a measured
accuracy multiplier or a demonstrated general porting advantage. It also does
not claim full upstream-library parity, complete static semantics, or a full C
ABI migration beyond each target's stated boundary.

## Verify the repository

From the repository root, run the portable full-evidence gate:

```bash
uv run python scripts/port_eval.py --all-gates --full
```

It rebuilds disposable graphs under `output/port_gates/`, runs each declared
source contract and Rust port evaluation, and reports named source-only gaps.
An unavailable optional tool or an opt-in check not requested produces an
explicit `SKIP`; an installed tool that fails makes the gate fail. See [the gate guide](examples/PORT_EVIDENCE.md)
for clean-machine prerequisites, the fast/pre-release split, and the opt-in
long differential and scale checks.

## Read in this order

| When you need… | Read… | Why |
| --- | --- | --- |
| A short orientation and one verification command | This README | The supported conclusion, its limit, and the target map below. |
| To operate or debug the portfolio gate | [examples/PORT_EVIDENCE.md](examples/PORT_EVIDENCE.md) | Manifest rules, clean-checkout behavior, tool skip policy, and CI tiers. |
| The project design, research inputs, and future frontier | [Plan.md](Plan.md) | The roadmap and design decisions; treat its current-status language as bounded by the write-up. |
| The results and their interpretation | [PHASE7_WRITEUP.md](PHASE7_WRITEUP.md) | What the ports and closed ablation series support, including the negative result. |
| Why current prose says exactly what it says | [docs/EVIDENCE_AUDIT_20260728.md](docs/EVIDENCE_AUDIT_20260728.md) | A dated claim → evidence → edit ledger, with runnable checks. |
| Raw ablation protocol and results | [PHASE7_ABLATION.md](PHASE7_ABLATION.md) | The closed experiment record; read it for tables or preregistration detail, not as a mutable how-to. |
| One target's source, scope, and exceptions | Its `examples/*/PROVENANCE.md` below | The local contract and provenance. cJSON and charset-normalizer also have separate mechanical API audits. |

**Proposed future cleanup, not performed here:** [Plan.md](Plan.md)'s current
evidence snapshot partly duplicates [PHASE7_WRITEUP.md](PHASE7_WRITEUP.md).
On a future roadmap revision, move the outcome/status narrative into the
write-up and leave the plan focused on design and future work. Do not merge this
README with `PORT_EVIDENCE.md`: one is an orientation, the other an operations
manual. Likewise, keep the cJSON and charset API audits separate from their
provenance records because the audits are mechanical evidence, not summaries.

## Target guide

| Target | Reader's view |
| --- | --- |
| `mini_game` | A small deterministic game simulation. Its Rust port covers the recorded simulation-state trace; the Python golden trace, fresh graph/context packs, and port evaluation prove that contract. It makes no claim beyond the exercised simulation behavior. |
| `mini_lang` | A compact language interpreter. Its Rust port covers the declared lexer/parser/evaluator golden contract, proved by the source contract, fresh graph/context packs, and port evaluation. It does not claim a general-purpose language implementation beyond that contract. |
| [`jsmn`](examples/jsmn/PROVENANCE.md) | A C JSON tokenizer/parser. Rust covers default-mode byte-oriented `jsmn_init`/`jsmn_parse`; a compiled C-oracle golden contract, fresh C graph, and port evaluation prove it. Alternate macro configurations and parent links are outside the port. |
| [`inih`](examples/inih/PROVENANCE.md) | A C INI parser. Rust covers default-configuration string parsing and recording-handler behavior, proved by C-oracle goldens and the port gate. File I/O, full C ABI/callback surface, custom allocation, and non-default option matrices remain out. |
| [`sqlparse`](examples/sqlparse/PROVENANCE.md) | A multi-module Python SQL package. Rust covers the lex → statement-splitting → optional-semicolon-strip path, with source goldens, lexer differential checks, fresh graph/context packs, and port evaluation. It does not claim the full grouping or formatting surface. |
| [`semantic_version`](examples/semantic_version/PROVENANCE.md) | A Python semantic-version library. Rust covers `Version`, `SimpleSpec`, and `NpmSpec`, proved by source goldens, graph/context packs, and port evaluation. Other upstream surface is not claimed. |
| [`diff_match_patch`](examples/diff_match_patch/PROVENANCE.md) | A Python diff/match/patch library. Rust covers the selected staged algorithmic core under its source contracts and port gate. It does not claim unscoped upstream behavior outside that selected core. |
| [`charset_normalizer`](examples/charset_normalizer_rust/PORT_STATUS.md) | A data-heavy Python encoding detector with a bounded Rust product surface. Targeted Rust/Python parity, a live source-oracle differential, API audit, and port gate prove the declared detector/API/codec scope. Stateful ISO-2022 extension profiles and the other itemized API exclusions remain explicit boundaries. |
| [`cJSON`](examples/cjson/PROVENANCE.md) | A C JSON tree library. Rust covers the closed exclusive-ownership parse/inspect/print/owned-mutation surface, proved by C-oracle traces under ASan, a header-derived API audit, compiler-anchored refusal candidates, and Miri properties. Source-sharing reference APIs and process-global allocator/error-state calls require a different representation or policy and are refused. |
| [`jsonpatch`](examples/jsonpatch/PROVENANCE.md) | An RFC 6902 source-only boundary case. Its Python apply contract, dynamic-dispatch diagnostics, and runtime registry oracle are evidence; no Rust port is claimed because the calls-only closure under-captures registry/polymorphic behavior for a fair graph-led port. |
| [`humanize`](examples/humanize/PROVENANCE.md) | A source-only number-formatting slice. Python goldens and an adequacy-clean graph support the closed ablation record, which found no graph advantage; there is no Rust port or Rust-port claim. |
| [`isodate`](examples/isodate/PROVENANCE.md) | A source-only duration parsing/formatting slice used by the closed ablation record. Its protected archive remains evidence of the experiment; there is no Rust port, and the aggregate gate deliberately does not regenerate the frozen archive. |

The gate also carries focused diagnostics rather than treating them as silent
implementation details: C preprocessor liveness is checked against compiler
output, Python registry extraction is checked against imported runtime objects,
runtime call observations are compared with published graph calls, and cJSON's
public surface/refusals are checked against its real header, C traces, and
compiler locations. Their scope and residuals are recorded in the target
provenance files and the [evidence audit](docs/EVIDENCE_AUDIT_20260728.md).
