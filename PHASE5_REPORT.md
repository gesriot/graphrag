# Phase 5 Evidence Report — Scaled, Multi-Package Python→Rust Port

**Date:** 2026-06-25. Frozen snapshot of the deterministic-graph + golden-contract
porting methodology, now validated at scale on a real external multi-package
library (the Plan Phase 5 criterion).

## Thesis (recap)

Deterministic source graph (tree-sitter + AST resolver: tiers/observations,
import/annotation/ctor tracking) is the ground truth; a verifiable, repeatable
harness drives Python→Rust ports. Two rails separate *means* from *ends*:

- `scripts/audit_call_edges.py` — graph quality: structural pass rate of CALLS
  edges, dangling targets, seeded precision sample, and an **import-aware**
  semantic-suspicion check.
- `scripts/port_eval.py` — end-to-end port: graph quality → context packs →
  `cargo fmt/check/test/run` → golden cases → manual-fix count, with contract
  coverage required per nested golden suite.

**Per-project gate:** (1) license captured → (2) golden/contract before any Rust
→ (3) graph clean (`semantic_suspicions == 0`) → (4) `port_eval` OVERALL PASS.

## Results — five Python→Rust ports, 0 manual fixes each

| Project | Class | Golden cases | manual_fixes | OVERALL |
|---|---|---|---|---|
| mini_game | greenhouse simulator | 5 | 0 | True |
| mini_lang | interpreter (lexer→parser→eval) | 28 | 0 | True |
| semantic_version | SemVer Version + SimpleSpec + NpmSpec | 147 | 0 | True |
| diff-match-patch | Myers diff + Bitap + patch | 107 | 0 | True |
| **sqlparse.split** | **scaled, multi-package pipeline** | **65** | **0** | **True** |

Each port matches its frozen golden contract exactly against the Python
reference (incl. Unicode-sensitive SQL token values, percent-encoding,
prerelease/build precedence, fuzzy patch apply, and splitter quirks).

## Phase 5 scale metrics (sqlparse 0.5.5, BSD)

- Source: 4,146 Python LOC across 21 modules, nested `engine/` + `filters/`
  sub-packages (real cross-package imports).
- Graph: 243 entities, 454 relationships, 253 call observations (index ~5s).
- Audit: 229 resolved calls, structural pass rate 1.0, 0 anomalies, 0 dangling,
  0 semantic suspicions (after the heuristic was made import-aware for
  `from pkg import module; module.func()`); manual precision sample 12/12.
- Bottleneck found at scale was **audit-heuristic precision, not the resolver** —
  the resolver stayed precision-clean across packages.

### `sqlparse.split` port (staged)

1. token type tree + 811 keyword entries (generated from Python, first-match-wins).
2. lexer parity — `fancy-regex` `captures_from_pos` + `start==pos` (= Python
   `re.match(text, pos)` with full lookbehind context); differential gate of
   341 tokens, 0 divergences.
3. `StatementSplitter` state machine + `Statement` stringification + `split()`.
4. `port_eval`: 65 golden cases (40 lexer + 25 split), OVERALL PASS True.

## Reproduce

```bash
uv run python scripts/index_python.py --package examples/sqlparse --graph byog_sqlparse --use-advanced
uv run python scripts/audit_call_edges.py --graph byog_sqlparse --json
uv run python scripts/port_eval.py --target sqlparse_split --graph byog_sqlparse \
  --source examples/sqlparse --port examples/sqlparse_rust \
  --symbol lexer:tokenize \
  --symbol engine.statement_splitter:StatementSplitter.process \
  --symbol engine.filter_stack:FilterStack.run
(cd examples/sqlparse_rust && cargo test --all-targets && cargo clippy --all-targets -- -D warnings)
uv run python -m pytest examples -q   # full suite (currently 348 passed)
```

## Caveats (honest scope)

- **Core scope, not full API:** `sqlparse.split` only — `parse`, `grouping`,
  `formatter` are out of scope.
- The graph has no entity for `__init__.py:split`, so the 3/3 context packs go
  through `lexer:tokenize`, `FilterStack.run`, `StatementSplitter.process`.
- diff-match-patch v1 supports `Diff_Timeout <= 0` semantics only (no half-match
  / deadline); semantic_version excludes `LegacySpec`/`SpecItem`/Django.
- "0 manual fixes" means the structure-preserving port compiled and matched
  golden without an iterative compiler/golden-failure loop (clippy-tidy edits
  aside). It is not a claim of full upstream API compatibility.

## Next (Plan Phase 6)

The remaining large unknown is a **different frontend / semantic source of truth**
(C/C++), not deeper Python. Phase 6 plan: a small permissive C target with tests
+ `compile_commands.json` → index + audit first (measure), then one bounded
component end-to-end through the same gate.
