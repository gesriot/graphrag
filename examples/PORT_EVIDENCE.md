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

`SKIP` means the optional tool is absent, or an opt-in check was not requested;
a present tool whose probe or command fails is `FAIL`, never a skip. `--full`
enables CJSON Miri and charset-normalizer's repository pytest. Add
`--differential-full` for charset-normalizer's long live oracle sweep and
`--scale` for its opt-in release-scale harness.

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
| `sqlparse` | Python split/lex contract → fresh Python graph/context packs → Rust contract | Complete Rust port |
| `semantic_version` | Python Version/SimpleSpec/NpmSpec contracts → graph/context packs → Rust contracts | Complete Rust port |
| `diff_match_patch` | Python diff/match/patch contracts → graph/context packs → Rust contracts | Complete Rust port |
| `charset_normalizer` | Rust/Python parity suite, live differential, graph/context packs, Rust contract | Complete bounded Rust port; extra sweeps are opt-in |
| `cjson` | Header audit, reached C/ASan/refusal traces, graph/context packs, Rust contract, optional Miri | Complete bounded Rust port; Miri is enabled by `--full` |
| `jsonpatch` | No Rust port; 25-case apply contract and `byog_jsonpatch` graph exist | Declared gap, not a passing port |
| `humanize` | No Rust port or Rust golden consumer | Declared gap, not a passing port |
| `isodate` | No Rust port | Declared gap; the protected `ablation_v3` archive and frozen graph are not run |
