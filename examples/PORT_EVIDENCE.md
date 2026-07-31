# Port evidence gate

Run every declared port profile from a clean checkout with:

```bash
uv run python scripts/port_eval.py --all-gates --full
```

The runner reads [`scripts/port_gates.json`](../scripts/port_gates.json) and
fails closed in both directions: if an `examples/*_rust/Cargo.toml` directory
lacks a profile, and if an `examples/` package carrying a golden contract has no
profile at all. The second check exists because `jsonpatch` — 25 golden cases
and a published graph, no Rust port — was omitted from the first version of this
manifest entirely, which is the failure mode a gap list is meant to prevent. A
profile reindexes into ignored `output/port_gates/`, audits that fresh graph,
generates context packs, runs the source oracle/contract command, then runs the
standard Rust `port_eval` stages. A corrupt golden therefore makes either the
source oracle or the Rust golden consumer fail and the command exits non-zero.

The default aggregate command is the **source-evidence** view: it is designed
to rebuild from Git and does not rely on a published graph. `--full` adds the
separate **local-artifact-health** view. It compares every declared mutable
published `byog_*` graph with a fresh in-memory extraction without rewriting the
published root; a present stale `current` snapshot is a failure, while an absent
local artifact is an explicit health skip. The frozen `byog_isodate` experiment
graph is declared as an exemption and is never read or reindexed by that stage.

`SKIP` means the optional tool is absent, or an opt-in check was not requested;
a present tool whose probe or command fails is `FAIL`, never a skip. `--full`
enables CJSON Miri and charset-normalizer's repository pytest. Add
`--differential-full` for charset-normalizer's long live oracle sweep and
`--scale` for its opt-in release-scale harness.

## Clean-checkout behaviour

The **port-profile stages** do not read or update published `byog_*`
directories. Every profile reindexes into ignored
`output/port_gates/<profile>/graph`; the aggregate run orders cJSON before the
repository-wide pytest so its regenerable documentation claim is audited from
that fresh graph. The full documentation-claim check additionally invokes the
combined oracle summary, whose JSONPatch call-observation residual deliberately
reads the published local `byog_jsonpatch` baseline. If that baseline is absent,
the live residual claim fails rather than substituting a fresh graph. The
separate published-graph-health stage reports absent mutable roots as `SKIP`,
but fails a root that exists and disagrees with the extractor. Frozen SQLParse
snapshots and the protected isodate ablation graph are deliberately not
regenerated. When those frozen artifacts are absent from a clean checkout, the
documentation checker prints a named `SKIP` for each while still checking the
recorded prose; it reports `PASS WITH SOURCE SKIPS`, never an unqualified pass.

[`docs/EVIDENCE_DURABILITY.md`](../docs/EVIDENCE_DURABILITY.md) is the
manifest-derived inventory of every local published graph reference: it names
which claims/oracles need a local artifact, which measurements the gate can
rebuild, and which records are historical rather than replayable. The full gate
also runs its `--check` mode, so a stale inventory is a failure rather than a
reader-only warning. The documents stay separate by design: this guide answers
which stages execute, while the durability inventory answers what a particular
artifact-backed claim means and whether it can be replayed from Git.

Bootstrap prerequisites are intentionally outside the gate: `uv` itself and
the locked Python dependencies must already be available (or resolvable from a
package mirror), and Cargo dependencies need a local cache, crates.io, or an
equivalent configured registry. A fresh machine therefore needs network access
unless those dependency caches are pre-populated. The C profiles additionally
need a working C compiler. An absent compiler or nightly Miri component is an
in-gate `SKIP`; a compiler or Miri installation that is present but broken is a
`FAIL`. Miri is nightly/toolchain/platform dependent, so `--full` may honestly
finish as `PASS WITH SKIPS` on a machine without that component.

## CI policy

Use the fast pre-commit evidence gate:

```bash
uv run python scripts/port_eval.py --all-gates
```

It runs every source contract, fresh graph/context-pack stage, and ordinary
Rust `port_eval` stage. It intentionally does not run repository-wide pytest,
cJSON Miri, charset-normalizer's exhaustive differential sweep, or its scale
harness; it can therefore miss cross-example/documentation regressions,
undefined-behaviour/aliasing regressions, and long-tail codec or performance
divergences.

Use the pre-release gate:

```bash
uv run python scripts/port_eval.py --all-gates --full --differential-full --scale
```

`--full` is the portable full-evidence tier (including the broad pytest suite
and Miri when available). The two extra flags are deliberately explicit because
they add the long live-oracle and scale checks rather than silently making every
developer invocation expensive.

The existing handoff commands remain supported:
`examples/cjson/tools/check_port.sh --full` and
`examples/charset_normalizer_rust/tools/check_port.sh --full`. They retain
their documented specialized modes; the manifest is the common gate for the
complete port set rather than another copy of either shell script.

| Profile | Evidence declared in the manifest | Current boundary |
| --- | --- | --- |
| `mini_game` | Python golden trace → fresh Python graph/context packs → Rust contract | Complete Rust port |
| `mini_lang` | Python golden contract → fresh Python graph/context packs → Rust contract | Complete Rust port |
| `jsmn` | C oracle contract → fresh C graph/context packs → Rust contract | Complete Rust port |
| `inih` | C oracle contract → fresh C graph/context packs → Rust contract | Complete Rust port |
| `sqlparse` | Python split/lex contract → inherited-member, direct-initializer, and re-export identity/reachability runtime audits → fresh Python graph/context packs → Rust contract | Complete Rust port; direct `__init__.py` definitions are graph entities. Re-export aliases remain a measured static-namespace residual, not duplicate nodes or closure edges; a separate local-graph call oracle measures the 65-case source corpus |
| `semantic_version` | Python Version/SimpleSpec/NpmSpec contracts → graph/context packs → Rust contracts | Complete Rust port |
| `diff_match_patch` | Python diff/match/patch contracts → graph/context packs → Rust contracts | Complete Rust port |
| `charset_normalizer` | Rust/Python parity suite, live differential, graph/context packs, Rust contract | Complete bounded Rust port; extra sweeps are opt-in |
| `cjson` | Header audit, reached C/ASan/refusal traces, graph/context packs, Rust contract, optional Miri | Complete bounded Rust port; Miri is enabled by `--full` |
| `jsonpatch` | No Rust port; 25-case apply contract and `byog_jsonpatch` graph exist | Declared gap, not a passing port |
| `humanize` | No Rust port or Rust golden consumer | Declared gap, not a passing port |
| `isodate` | No Rust port | Declared gap; the protected `ablation_v3` archive and frozen graph are not run |
