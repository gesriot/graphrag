# Independent-oracle contract (2026-07-29)

This is a behavioral contract, not a shared oracle framework. The four tools
measure different things, retain separate implementations, and must not collapse
their categories into one score. Their common duty is to make the boundary of a
claim as inspectable as the claim itself.

## Rules, with the bugs that made them necessary

| Required behavior | What it means here | Scar to inspect |
| --- | --- | --- |
| **Independently enumerate the comparison population.** | Do not grade only candidates the system under test already named. Read the C compiler, imported Python runtime, published graph, or parsed C header as appropriate. | `f88426a`: `jsonpatch` was absent from a hand-maintained gap list. `717d2e4`: a decorator-filled runtime registry was invisible because the AST never named it. |
| **Keep populations disjoint.** | Report agreement, miss, unknown, vacuous, false-positive, and coverage populations separately; an unjudgeable item is never an agreement. | `5dfdf58`: empty-body C regions inflated an agreement rate. `717d2e4`: 350 non-callable table entries were incorrectly called missed dispatch targets. |
| **Never report a rate over an empty population.** | Use `n/a`, not `1.0` or `100%`, when no comparable item was judged. | `717d2e4`: registry packages with nothing scored looked perfect. The C liveness oracle had the same defect until the 2026-07-29 regression test. |
| **Disclose the corpus/configuration actually examined.** | State the compilation inputs, runtime registries, header, published graph, and workload cases; never silently replace a missing corpus with a convenient sample. | `b67eadd`: the humanize call oracle silently used an eight-call smoke fallback and could execute zero golden cases. |
| **Fail loudly rather than substitute.** | A missing or broken oracle input is an error, or an explicitly named tool-level skip where that tool defines one; it is never empty agreement or a fallback measurement. | `b67eadd`: a missing golden now refuses to substitute a smoke corpus. `f88426a`: a broken compiler is a failure, not a skip. |
| **Be falsifiable by a planted defect.** | A deliberately wrong label, candidate, graph edge, header entry, or diagnostic location must move the result to the corresponding bad category. | `5dfdf58`: an E0502 from an unrelated helper once satisfied the cJSON proof until the primary span was checked. `d5be293` and `b67eadd` add wrong-candidate and add/remove-edge plants. |

Run `git show <commit>` for the complete incident records. The commit ids above
are intentionally part of this contract: the rules are not abstract style
preferences; they are regressions that already happened here.

## Compliance review

| Oracle | Independent population | Disjoint populations / empty rates | Corpus and failure behavior | Falsifiable plant | Current assessment |
| --- | --- | --- | --- | --- | --- |
| C preprocessor liveness | `cc`/`clang -E` line survival and `-dM` macro state, not the labeler | live/dead scored; unknown and vacuous separate; zero scored is now `n/a` | reports compile commands and translation-unit files; compiler failure raises | flipped line and macro labels become disagreements | Satisfies contract after the 2026-07-29 `n/a` fix. |
| Python registry | subprocess import plus independent runtime discovery | agree/disagree/missed/false-positive/undetected separate; empty scored population is `n/a` | identifies registries and import failures; an import failure is named, not agreement | injected wrong Name/decorator candidate disagrees | Satisfies contract. Lambda values remain a measured residual, not guessed targets. |
| Call-graph observation | `sys.setprofile` over the real golden workload versus the published graph | confirmed/missed/unconfirmed separate; coverage and recall are `n/a` if their denominator is empty | prints every workload file and executed case count; missing golden refuses a substitute | deleting a graph edge becomes missed; fabricating one becomes unconfirmed | Satisfies contract. `unconfirmed` is coverage, not an extractor defect. |
| cJSON API surface | parses `cJSON.h`; its linked audit tests execute named C traces and compile candidate Rust snippets | covered, ownership-blocked, and process-global exclusions partition the header; no rate is claimed | stale audit or unclassified header entry fails; linked tests fail on a missing trace or mismatched compiler span | header corruption, unreachable refusal scenario, and unrelated E0502 plants fail | Satisfies contract. It is a classification audit, so a rate is not applicable. |

**Review result:** all four currently satisfy every applicable rule. The only
violation found by this review was the C liveness oracle's empty-population rate;
it mattered because a package with no judged regions could have appeared fully
verified, and is now fixed and regression-tested. cJSON's lack of a rate is not
a gap: it is an exhaustive classification rather than a rate-bearing comparison.

## One command, native reports preserved

```bash
uv run python scripts/oracle_summary.py
uv run python scripts/oracle_summary.py --json
```

The adapter invokes the three JSON command interfaces and cJSON's public
`--check` entry point. It does not share a scorer or substitute an unavailable
comparator. A child failure makes the summary fail with the child command and
its output; residuals remain visible without being converted into failures.

## Current residuals

The following values are derived by the combined command, not copied from a
historical report. They are boundaries to inspect, not evidence of a clean pass.

| Oracle | Current residual | Interpretation |
| --- | --- | --- |
| C preprocessor liveness | **9 vacuous** regions; 0 unknown | Directive-only branches that neither line survival nor unique macro attribution can judge. They are outside the agreement rate. |
| Python registry | **25 missed** isodate lambda entries | The runtime callable values exist, but their lambda bodies have no honest single callee name; they remain unguessed. |
| Call-graph observation | **3 missed** observed jsonpatch edges; **83 unconfirmed** graph edges | Registry Name-table members at labelled dispatch sites are promoted to non-deterministic `calls` edges, and same-file inheritance is carried by a separate `inherits` type rather than overloading `calls`. The three remaining misses are the `_ops` property read, `_ops → _get_operation` through `map`, and the if/else `from_ptr`. Unconfirmed edges are not exercised by this workload, not proven wrong. |
| cJSON API surface | **6 ownership-blocked** calls; **4 process-global exclusions** | The six need aliased/shared mutable storage under the observed C traces; the four need allocator/error-state policy. Neither is silently deferred. |

The older jsonpatch table with 25 missed edges remains a dated baseline in its
provenance record. The combined command intentionally reports the active
**current-local** published graph instead: it is not a frozen historical
snapshot, and the full portfolio gate health-checks a present `byog_jsonpatch`
against the current extractor before that evidence is relied on. If the local
baseline is absent, the residual claim fails rather than substituting a fresh
graph. If these numbers move, update this table and its live documentation
claim together.
