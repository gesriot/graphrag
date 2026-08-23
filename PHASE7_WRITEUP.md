# Phase 7 write-up – what worked, what did not, why the graph is still useful

**Audience:** a reader outside this project. This is the entry document for Phase
7: what the system is, what the ablation series measured, and what the evidence
actually supports. Detailed tables and preregistration notes live in linked
files; this document does not replace them.

**Related:** [Plan.md](Plan.md) · [PHASE7_ABLATION.md](PHASE7_ABLATION.md) ·
[PHASE5_REPORT.md](PHASE5_REPORT.md) · [README.md](README.md) · per-target
`examples/*/PROVENANCE.md` · product CLI `graphrag-code` / `python -m graphrag_code` (source-checkout: `scripts/graphrag_code.py`)

**Series status:** the graph-vs-raw ablation series is **closed** (2026-07-25)
with a **negative** finding on the headline capability claim. See
[§2](#2-what-did-not-work-at-full-strength) and the series-close note in
[PHASE7_ABLATION.md](PHASE7_ABLATION.md).

---

## 1. What was built and what it demonstrably does

### 1.1 The pipeline (means)

This project builds a **deterministically generated** code knowledge graph for
the covered source languages/configurations and uses it in a verification-gated
porting process. “Deterministic” describes the extractor inputs and rules, not
complete static semantics or a proof that every high-confidence edge is correct.
It is not a claim that generic LLM entity extraction over raw text recovers
precise call structure. The intended path is:

1. **Deterministic extraction** – tree-sitter (and language-specific AST
   resolution) for Python (`scripts/extract_python.py`, `scripts/index_python.py`)
   and C (`scripts/extract_c.py`, `scripts/index_c.py`).
2. **BYOG interchange** – GraphRAG-compatible parquet:
   `entities` / `relationships` / `text_units`, plus `call_observations` for
   weak or ambiguous calls. Snapshots under a graph root (`current` +
   `snapshots/`). See [Plan.md](Plan.md) §2 and the BYOG helpers in
   `scripts/byog_graph.py`.
3. **Provenance and confidence** – edges and nodes carry source location,
   extractor identity, confidence, and `is_deterministic` so a human or agent
   can tell hard fact from observation.
4. **Graph-side gate** – `scripts/audit_call_edges.py`: structural pass rate of
   `calls` edges, dangling targets, semantic-suspicion heuristics (including an
   import-aware check), and a seeded precision sample.
5. **Local queries and context packs** – `scripts/graph_query.py` (callers,
   callees, neighbors, impact, dependency order, symbol, observations) and
   `scripts/context_pack.py` (entity + neighbors + text units + first-class
   `uses_data` / `data_dependencies` when present).
6. **Golden-first porting gate** – before Rust: license/provenance, a golden
   contract the **reference language** already passes, then a clean graph audit,
   then porting. Recorded in [Plan.md](Plan.md) (“Porting gate”).
7. **End-to-end harness** – `scripts/port_eval.py`: graph quality → context packs
   → `cargo fmt/check/test/run` → golden contract coverage → `manual_fix_count`
   → `OVERALL PASS`.

Optional later: official GraphRAG LLM workflows (community reports, global/local
search). They are **not** required for the default path
([Plan.md](Plan.md): “no external API by default”).

A single product entry point over these scripts is the installable
`graphrag-code` console command (`python -m graphrag_code`; source-checkout
`scripts/graphrag_code.py`). The scripts themselves remain the stable
automation layer ([README.md](README.md)). The wheel does not bundle
published `byog_*` graphs or port evidence; `port-eval` stays checkout-only.

### 1.2 What the ports demonstrate

The **north-star** in [Plan.md](Plan.md) is Python→Rust fidelity under
verification, not “the graph made the model smarter.” Against that bar, the
repo records bounded results under declared contracts:

**Eight ports** are named in [Plan.md](Plan.md) as end-to-end validated with
`overall_pass=True` and **0 recorded manual fixes**:

| Target | Class (as recorded) | Golden cases (as recorded) | Source |
|---|---|---|---|
| `mini_game` | greenhouse / simulator | 5 | [PHASE5_REPORT.md](PHASE5_REPORT.md) |
| `mini_lang` | interpreter (lexer→parser→eval) | 28 | [PHASE5_REPORT.md](PHASE5_REPORT.md) |
| `semantic_version` | Version + SimpleSpec + NpmSpec | 147 | [PHASE5_REPORT.md](PHASE5_REPORT.md), [Plan.md](Plan.md) |
| `diff-match-patch` | Myers diff + Bitap + patch | 107 | [PHASE5_REPORT.md](PHASE5_REPORT.md), [Plan.md](Plan.md) |
| `sqlparse.split` | multi-package lexer + splitter | 65 | [PHASE5_REPORT.md](PHASE5_REPORT.md), [Plan.md](Plan.md) |
| `jsmn` | C tokenizer (default mode) | 18 | [Plan.md](Plan.md), `examples/jsmn/PROVENANCE.md` |
| `inih` | C string parser (default config) | 21 | [Plan.md](Plan.md), `examples/inih/PROVENANCE.md` |
| `cJSON` | C ownership slice + float-print + mutation/builder | **59** live on 2026-07-25 (`port_eval`, 3 golden files): 22 ownership + 30 float-print + 7 mutation traces | [Plan.md](Plan.md), `examples/cjson/PROVENANCE.md` |

**Plus** the advanced stress-test **`charset-normalizer`**: graph audit pass rate
1.0, 18/18 captured golden samples, 83 Rust tests, handoff via
`examples/charset_normalizer_rust/tools/check_port.sh`
([Plan.md](Plan.md), `examples/charset_normalizer_rust/PORT_STATUS.md`). It is
scoped as a data-table-heavy validation target, not full upstream parity.

**What “0 manual fixes” means** ([PHASE5_REPORT.md](PHASE5_REPORT.md)): the
structure-preserving port compiled and matched its frozen golden without an
iterative compiler/golden-failure loop that rewrites the design mid-flight. It
does **not** mean zero human engineering in building the port, zero scope
limits, or that the graph caused the success versus raw prompting.

**What it does not mean:** that any of these ports would have failed without a
graph; that full library APIs were ported; that C/C++ production migration is
ready (clang-accurate macros/types, multi-config builds, full ABI – still open
per [Plan.md](Plan.md)); or that the Microsoft “code4llm” demo was reproduced
(see §1.4).

### 1.3 Scale snapshot (sqlparse – the Phase 5 scale target)

From [PHASE5_REPORT.md](PHASE5_REPORT.md) and [Plan.md](Plan.md) (sqlparse 0.5.5):

- ~4.1k Python LOC, 21 modules, nested packages  
- Graph (recorded): 243 entities, 454 relationships, 253 call observations  
- Audit (recorded): 229 resolved calls, structural pass rate **1.0**, 0 anomalies,
  0 dangling, 0 semantic suspicions after the import-aware heuristic; seeded
  precision sample 12/12  
- Port: `port_eval` 65 golden cases, 3/3 context packs, `manual_fixes=0`,
  `OVERALL PASS=True`

**Live re-check (2026-07-25):**  
`uv run python scripts/audit_call_edges.py --graph byog_sqlparse` reported
**230** calls, structural pass rate **1.0**, anomalies/dangling/semantic_suspicions
**0**.

The 229-vs-230 difference is not drift or a stale document: `byog_sqlparse` holds
two snapshots and both numbers are correct for the one they describe.

| snapshot | entities | relationships | calls | |
|---|---|---|---|---|
| `20260618-151436` | 243 | 454 | 229 | the frozen Phase 5 baseline quoted in `PHASE5_REPORT.md` and `Plan.md` |
| `20260625-154143` | 283 | 537 | 230 | current index, after module-level `data` entities and 42 `uses_data` edges were added in the post-v1 packer fix |

Quote the baseline when citing Phase 5 evidence and the current index when
reporting a live audit; do not silently mix them.

### 1.3b Scale snapshot (cJSON – Phase 6 ownership + mutation graph)

From [examples/cjson/PROVENANCE.md](examples/cjson/PROVENANCE.md) and [Plan.md](Plan.md):

The cJSON BYOG has two load-bearing scopes. Quote the one the claim needs.

| scope | entities | relationships | calls | observations | |
|---|---:|---:|---:|---:|---|
| Full graph **current** (`20260726-040744`, library + mutation golden runner) | 145 | 637 | **495** | 144 | live audit / `port_eval` graph stage / `doc_claims` `cjson_graph_calls` |
| Library subgraph only (`cJSON:` → `cJSON:`) | 125 | – | **188** | – | ownership and ported-API claims |
| Pre-mutation-runner snapshot (bootstrap / provenance-stamp era) | 131 | 367 | **239** | 125 | historical; ownership slice before mutation traces |

The +14 entities / +256 calls from 239 → 495 are entirely
`runner:mutation_*` / `runner:trace_*` helpers in `tests/parse/runner.c`. The
library subgraph is identical across those snapshots. Preprocessor detection
flags 0/495 trusted call edges on the current full graph (and 0/188 on the
library).

### 1.4 Framing relative to the Russinovich / GraphRAG story

[Plan.md](Plan.md) §1 is careful: Microsoft GraphRAG is open source; the specific
“code4llm” demo and internal code-processing infrastructure shown in talks are
**not** publicly released (community issue closed as not planned). This project
implements a **public related pattern** – deterministic code graph + agents +
verification – it does **not** claim to have reproduced the talk demo, its
capabilities, or any internal Microsoft system. Match that care when citing this
work.

### 1.5 Full examples suite

Recorded expectation in [Plan.md](Plan.md) and several provenance docs:
`1898 passed, 2 xfailed` for `PYTHONPATH=. uv run pytest examples -q`
(includes the documentation-consistency check and C preprocessor provenance tests).

The product CLI is installable as `graphrag-code` / `python -m graphrag_code`
from the project wheel. Source-checkout `scripts/*.py` commands remain
compatible. The wheel does not bundle published `byog_*` graphs or
`examples/`; `port-eval --all-gates` stays a repository evidence gate.
Cooperating snapshot readers take a shared advisory lock on the
graph-root `.publish.lock`. MCP rejects managed roots without that regular
lock file; legacy immutable evidence remains available only through explicitly
unleased compatibility reads. This is not a distributed lease service.
`graphrag-code adopt-publication-lock` (also `python -m
graphrag_code.adopt_publication_lock` and
`scripts/adopt_publication_lock.py`) is the explicit offline way to add
that file to a pre-lock managed graph without reindexing. Creating the
lock requires `--offline-confirmed` because the program cannot see
legacy readers or publishers that ignore `.publish.lock`. Automatically
touching the file from MCP or the doctor would be unsafe: it would
serialize only against lock-aware peers while any still-running pre-lock
reader stayed unprotected, or a pre-lock publisher could replace
`current` without waiting. Adoption creates only `<graph>/.publish.lock`
after a compatibility-mode persisted-integrity doctor; payload, `current`,
and snapshots stay unchanged unless a separately running actor changes them.
The JSON result reports that observed pre/post comparison as
`payload_unchanged`. MCP remains strict. Checked-in `byog_*` roots stay
unleased until an operator adopts a disposable or operational copy.
`graphrag-code snapshot-history` and `snapshot-diff` (also
`python -m graphrag_code.snapshot_compare` and
`scripts/snapshot_compare.py`) expose bounded local snapshot history and
structural persisted-row diffs. They hold one shared `.publish.lock`
lease, resolve `current` once, and never create the lock. MCP adds
`snapshot_history` and `snapshot_diff` to the fixed 11-tool set and stays
strict. `--allow-unlocked-legacy` is CLI-only compatibility for
immutable pre-lock evidence and reports that there is no retention
guarantee. Row modification means canonical persisted fields differ;
this is not semantic equivalence. Missing fields differ from explicit
nulls, and JSON booleans differ from numbers. Staging directories are
notices, not history; notices keep the exact count and return at most 20
names. Shared leases protect only cooperating processes.
`graphrag-code snapshot-activate` (also `python -m
graphrag_code.snapshot_activate` and `scripts/snapshot_activate.py`) is
the explicit mutating CLI that retargets only `current` onto a retained
published snapshot. `--activate-confirmed` and `--expected-current` are
mandatory. It holds one exclusive existing-lock lease, never creates
`.publish.lock`, and is intentionally absent from the fixed 11-tool MCP
set. It does not delete, retain, publish, repair, or reindex. Advisory
locks do not protect against non-cooperating programs.
Query, context-pack, `graph_status`, and `graph_doctor` accept an
optional retained-snapshot selector (`--snapshot <id|current>` in the
CLI; MCP `snapshot="current"` on the existing nine selectable tools).
Historical reads do not require `snapshot-activate` and do not change
`current`. One shared `.publish.lock` lease pins the selected snapshot
against cooperating keep-last retention until the complete response is
built. `current` is resolved exactly once when selected and is not read
for an explicit published id. The MCP tool set remains exactly 11.
Explicit query/context CLI selectors require the existing regular
publication lock and never create it. Their omitted `--snapshot` path
keeps the existing default current/legacy-flat pre-lock compatibility
and has no retention guarantee. ``graphrag-code snapshot-pins`` (also
`python -m graphrag_code.snapshot_pins` and `scripts/snapshot_pins.py`)
lists operator, claim, and effective pins. ``snapshot-pin`` /
``snapshot-unpin`` write only ``.snapshot-pins.json`` after confirmation
and a registry-revision compare-and-swap. This is retention metadata, not
activation, backup, or replication. Listing never creates the file.
Unpin does not delete immediately. Cooperating keep-last protects
``current``, existing doc-claim pins, and operator pins. A malformed
registry aborts publication before ``current`` or snapshot deletion.
MCP remains exactly 11 read-only tools. ``graphrag-code
snapshot-retention-plan`` (also ``python -m
graphrag_code.snapshot_retention`` and
``scripts/snapshot_retention.py``) is a read-only report of the shared
keep-last selection helper used by cleanup. It does not prune, apply,
or delete. An absent pin registry stays absent. Malformed registry
state fails closed. The command is intentionally absent from MCP.
``graphrag-code snapshot-prune`` (also ``python -m
graphrag_code.snapshot_prune`` and ``scripts/snapshot_prune.py``)
applies exactly one CAS-verified plan under an exclusive existing-lock
lease. ``--prune-confirmed`` and ``--expected-plan-revision`` are
mandatory; there is no dry-run. A stale revision changes nothing.
Recursive deletion is not transactionally atomic. A partial prune
reports ``partial=true`` and requires a fresh plan; there is no
rollback. The command is intentionally absent from MCP.
``graphrag-code snapshot-staging`` (also ``python -m
graphrag_code.snapshot_staging`` and
``scripts/snapshot_staging.py``) is a read-only structural inventory of
``snapshots/.staging-*`` entries. It holds one shared existing-lock
lease and never creates ``.publish.lock``. Cooperating publishers hold
a dedicated advisory writer lease on
``snapshots/.staging-<id>/.staging-writer.lock`` while constructing
payload files. In an already-managed graph, reacquiring existing
writer-lock metadata briefly uses a shared graph-lock gate and releases
it before payload construction; reacquisition is nonblocking while gated,
and fresh publisher lock creation is not gated. Cleanup therefore cannot
remove the lock out from under a waiting cooperative writer while staging
writes remain concurrent. The exclusive graph-root publication lease is
otherwise used only for promotion. Inventory observes writer-lease contention with a
nonblocking probe. That observation is not ownership, writer death, or
cleanup eligibility. Missing lock metadata is legacy/unverifiable.
Two-scan agreement is bounded change detection, not a liveness lease
over a staging writer. A stable listing is not proof that a writer is
dead. No age heuristic is used. This inventory does not apply cleanup.
``staging_revision`` is informational and is not accepted or applied.
Inventory ``cleanup_eligible`` stays false.
``graphrag-code snapshot-staging-cleanup-plan`` (also ``python -m
graphrag_code.snapshot_staging_cleanup_plan`` and
``scripts/snapshot_staging_cleanup_plan.py``) is a read-only schema-2
plan over that inventory. Schema 1 was read-only/pre-apply
(``apply_supported=false``) and is not accepted by apply. Schema 2
sets ``apply_supported=true`` and keeps ``cleanup_applied=false``.
It reuses the two-scan scanner under one shared existing-lock lease
and never mutates staging or lock metadata. ``deletion_candidates``
is not ownership, writer death, or permission to delete. Observed
non-contention is not the apply command's exclusive writer-lock
claim. ``staging_state_revision`` hashes the internal consistency
token, including inodes, so a replacement that leaves public
inventory fields equivalent still changes the plan.
``plan_revision`` binds the decision inputs. This command does not
accept or apply that token. ``graphrag-code snapshot-staging-cleanup``
(also ``python -m graphrag_code.snapshot_staging_cleanup`` and
``scripts/snapshot_staging_cleanup.py``) is the explicit CAS apply:
it requires ``--cleanup-confirmed`` and
``--expected-plan-revision``, holds one exclusive existing-lock
lease, claims every selected existing writer lock before the first
deletion, and deletes only the recomputed candidates. Recursive
deletion is not transactionally atomic. A partial result requires a
fresh plan; there is no rollback. Both commands are intentionally
absent from MCP.
``graphrag-code snapshot-maintenance-plan`` (also ``python -m
graphrag_code.snapshot_maintenance_plan`` and
``scripts/snapshot_maintenance_plan.py``) embeds those two current
plans under one shared existing-lock lease. It does not prune, clean
staging, or apply. ``graphrag-code snapshot-maintenance-apply``
(also ``python -m graphrag_code.snapshot_maintenance_apply`` and
``scripts/snapshot_maintenance_apply.py``) is the CAS apply for that
composite: it requires ``--maintenance-confirmed`` and
``--expected-maintenance-revision``, holds one exclusive existing-lock
lease, claims every selected existing writer lock before the first
deletion, and applies staging cleanup then prune. Recursive deletion
is not transactionally atomic. A partial result requires a fresh
plan; there is no rollback. ``graphrag-code snapshot-maintenance-reconcile``
(also ``python -m graphrag_code.snapshot_maintenance_reconcile`` and
``scripts/snapshot_maintenance_reconcile.py``) is the read-only
aftermath inspection for a saved composite plan and optional apply
result. Before graph inspection it validates the composite and both
embedded self-hashes, direct candidate names, and exact schema-1 apply
outcomes. It then holds one shared existing-lock lease, never mutates,
and does not claim recovery or deletion cause.
``graphrag-code snapshot-export-plan`` (also ``python -m
graphrag_code.snapshot_export_plan`` and
``scripts/snapshot_export_plan.py``) is a read-only inspection of one
retained published snapshot's direct envelope payload files. It does
not export, archive, or authorize deletion.
``graphrag-code snapshot-export-apply`` (also ``python -m
graphrag_code.snapshot_export_apply`` and
``scripts/snapshot_export_apply.py``) is the CAS-guarded copy of that
payload set into a newly created destination. It does not mutate the
graph, overwrite an existing destination, or claim backup or
recoverability. A failure after atomic publication reports
``partial=true`` and never deletes the destination.
``graphrag-code snapshot-export-verify`` (also ``python -m
graphrag_code.snapshot_export_verify`` and
``scripts/snapshot_export_verify.py``) rechecks one standalone
export directory against the same canonical export_revision. It
does not inspect a graph or mutate the export, and it does not
claim backup or recoverability.
``graphrag-code snapshot-export-reconcile`` (also ``python -m
graphrag_code.snapshot_export_reconcile`` and
``scripts/snapshot_export_reconcile.py``) observes one standalone
destination against a saved export plan and optional saved apply
result. It does not inspect a graph, mutate the destination,
recover, or prove that apply created or deleted the path.
``graphrag-code snapshot-export-apply`` creates
``.export-writer.lock`` immediately after anchoring private staging
and holds an exclusive advisory writer lease through payload
construction and staged verification. The pathname is removed while
the lease is still held, then the lease is released before atomic
publication. The published destination never contains that lock file.
``graphrag-code snapshot-export-staging`` (also ``python -m
graphrag_code.snapshot_export_staging`` and
``scripts/snapshot_export_staging.py``) inventories direct
``.graphrag-export-*`` children under one selected parent. For
recognized real directories it may observe
``.export-writer.lock`` as ``metadata_absent``,
``metadata_unsafe``, ``held_at_scan``, or ``not_held_at_scan``. It does
not inspect a graph, infer ownership or writer activity, plan
cleanup, or delete anything. Inventory ``cleanup_supported`` stays
false. ``graphrag-code snapshot-export-staging-cleanup-plan``
(also ``python -m
graphrag_code.snapshot_export_staging_cleanup_plan`` and
``scripts/snapshot_export_staging_cleanup_plan.py``) is a
read-only schema-2 classification of those leftovers. Schema 1
was read-only/pre-apply (``apply_supported=false``) and is not
accepted by apply. Schema 2 sets ``apply_supported=true`` and
keeps ``cleanup_applied=false``. A candidate
requires a recognized real directory whose writer-lock metadata is
present, empty, restrictive-mode, single-linked, and
``not_held_at_scan`` on both agreeing scans. Prefixed
non-candidates are blocked. ``deletion_candidates`` is not
authorization to delete. The plan does not accept an expected
revision or confirmation. ``graphrag-code
snapshot-export-staging-cleanup`` (also ``python -m
graphrag_code.snapshot_export_staging_cleanup`` and
``scripts/snapshot_export_staging_cleanup.py``) is the separate
CAS apply. There is no dry-run and no graph lease. Confirmation
and a matching schema-2 ``plan_revision`` are required even for
an empty candidate set. Apply claims every selected existing
writer lock before the first deletion. Recursive deletion is not
transactionally atomic. A partial result always requires a fresh
schema-2 plan. No ownership, liveness, backup, authenticity, or
recovery is claimed. ``graphrag-code
snapshot-export-staging-cleanup-reconcile`` (also ``python -m
graphrag_code.snapshot_export_staging_cleanup_reconcile`` and
``scripts/snapshot_export_staging_cleanup_reconcile.py``) is the
separate observation-only aftermath inspection of a saved
schema-2 cleanup plan and optional saved schema-1 apply result.
It does not mutate, claim a writer lease, inspect a managed
graph, recover, or prove that apply deleted or failed to delete
a name. A fresh schema-2 cleanup plan is required before any
later apply.
``graphrag-code snapshot-import-plan`` (also ``python -m
graphrag_code.snapshot_import_plan`` and
``scripts/snapshot_import_plan.py``) is a read-only plan for
adding one standalone snapshot export to an existing managed
graph. It does not import, copy, activate, or mutate either
tree. It proves only the language-independent stored snapshot
envelope and observed bytes, and it does not run a Clang overlay
audit. An already-published matching id is still blocked. A
future import apply command is outside this milestone. The
composite plan, apply, reconcile, export-plan, export-apply,
export-verify, export-reconcile, export-staging,
export-staging-cleanup-plan, export-staging-cleanup,
export-staging-cleanup-reconcile, and import-plan commands
are intentionally absent from MCP.
Advisory locks do not protect against non-cooperating programs. This is
not natural-language search, an HTTP service, repair, reindex, or
semantic equivalence.

**Live re-check (2026-07-26):** `538 passed, 2 xfailed`.

**Live re-check (2026-07-29):** `538 passed, 2 xfailed` after two oracle
regression tests were added.

### 1.6 Evidence-boundary update (2026-07-28)

This update supersedes any broader present-tense capability wording elsewhere in
the plan. The runnable aggregate command is
`uv run python scripts/port_eval.py --all-gates --full`. Its manifest is checked
in both directions: every discovered Rust port has a profile and every source
package with a golden contract is a port or named gap. It currently declares
**9 port profiles and 3 named source-only gaps**; a gap is reported, not counted
as a passing port.

The current C evidence is stronger, but also narrower, than “safe Rust cannot
do this.” cJSON's header-derived audit records **78 public functions: 68
covered, 6 blocked by the current exclusive-`Box` representation, and 4
process-global allocator/error-state exclusions**. Every one of the six has a
reached C trace under ASan and a checked compiler candidate whose primary E0502
span is the later C-observable mutation; the audit names the shared-node,
shared-byte, or handle redesign that would close it. That proves the safe-borrow
boundary of this representation, not an impossibility claim about Rust.

Two other checks are evidence rather than rhetoric. The C preprocessor tests
compare scored liveness labels with independent `cc`/`clang -E` output while
keeping unknown regions out of the agreement count. The Python registry oracle
imports the actual package in a subprocess: the tested Name-valued and
decorator registrations must agree, a planted wrong candidate must disagree,
and lambda-valued tables remain explicitly missed rather than guessed. Neither
mechanism promotes a heuristic observation into a deterministic call edge.

Together these gates prove traceability, bounded behavior, and fail-closed
coverage for the named targets. They do not demonstrate a general graph-driven
porting advantage over raw source.

---

## 2. What did not work, at full strength

### 2.1 The headline hypothesis

**Claim under test:** a deterministic semantic graph gives a cold porting agent a
**measurable advantage** over handing it the raw source alone (capability and/or
efficiency).

**Result:** **not demonstrated** for the class of target this series used –
bounded, statically structured, single-entry-point library slices. After four
pre-registered attempts, the accumulated evidence **argues against** that claim
for this class ([PHASE7_ABLATION.md](PHASE7_ABLATION.md), series closed
2026-07-25).

A reader does not need the full ablation log to take away the finding:

> Ablations did not demonstrate that deterministic graph context improves
> cold-agent porting accuracy over raw source for bounded, clean benchmark
> slices. The graph remains valuable as an auditability, provenance,
> adequacy-gating, and context-assembly discipline – not as a measured accuracy
> multiplier.  
> – [PHASE7_ABLATION.md](PHASE7_ABLATION.md), “Series closed”

### 2.2 Four experiments (summary only)

Full tables, kits, and preregistration: [PHASE7_ABLATION.md](PHASE7_ABLATION.md),
[PHASE7_HUMANIZE_V2_PREREG.md](PHASE7_HUMANIZE_V2_PREREG.md),
[PHASE7_ISODATE_V3_PREREG.md](PHASE7_ISODATE_V3_PREREG.md).

| Experiment | Target | Design intent | Headline outcome |
|---|---|---|---|
| **v1** | `sqlparse.split` (familiar, multi-file) | First cold graph-vs-raw | Both arms high fidelity; graph **near-parity** with less material / tools; **efficiency**, not capability. Corrected protocol later: raw perfect, graph median 23/25 |
| **jsonpatch** | `apply_patch` slice | Fresh target | **Not a fair capability run** – call graph under-captures registry + polymorphism; boundary documented instead of forced |
| **v2** | `humanize.number` (fresh, N=3) | Less prior, multi-formatter | Near-parity (medians **59/59** graph vs **58/59** raw); **no** capability or efficiency win |
| **v3** | `isodate.parse_duration` (N=3, **GPT-5.6**) | High raw-assembly cost (8 modules), adequacy-clean | Medians **24/24** both arms; raw perfect ×3; **no** capability or efficiency win |

**Interpretive correction (2026-07-28):** The preceding v1 row is a historical
record of its early observation. The corrected protocol did not establish an
efficiency win, so the closed-series conclusion is no measured graph advantage
in accuracy or efficiency for the tested benchmark class. This correction does
not alter the archived run record.

**v3 detail (harness-measured scores)** – from
[PHASE7_ABLATION.md](PHASE7_ABLATION.md):

| arm | scores | median |
|---|---|---|
| arm_graph (13 packs) | 24, 23, 24 | **24/24** |
| arm_raw (10-file package) | 24, 24, 24 | **24/24** |

Isolation evidence (`verify-fill`) clean on all six runs; artifacts under
`examples/isodate/ablation_v3/`.

**Live re-check of the v3 mini-gate (not a re-run of N=3):**  
`uv run python scripts/ablation.py adequacy --graph byog_isodate --spec scripts/ablation_specs/isodate_adequacy.json`  
→ `adequate: true`, `closure_size: 16`, 13/13 must-reach, 0 leaked.

### 2.3 Structural reason (why this class of experiment cannot show the claim)

From the series conclusion in [PHASE7_ABLATION.md](PHASE7_ABLATION.md):

**In this series, every slice small enough to be a clean, adequacy-gated
benchmark was also small enough for the raw package to fit in the model’s
context.** The targets tried are on the order of a few thousand LOC (or less)
with one obvious entry point. “Raw-assembly cost” was *locating and wiring* code
the model could still read in full – not *being unable to see the code*. This
does not establish the same result for larger repositories or hard material
budgets; it identifies the regime the closed experiment did not reach.

So the negative result is not “the graph is useless.” It is: **on these
protocol runs, the graph arm did not improve the measured port outcome over the
raw arm.**

### 2.4 The jsonpatch boundary (not a failed ablation – a documented frontier)

`examples/jsonpatch/PROVENANCE.md` and [PHASE7_ABLATION.md](PHASE7_ABLATION.md):
after adding general resolver edges (chained constructors, same-file ctor/factory,
property bridge), the apply-slice closure still stalls on higher-order / dynamic
patterns (`map(self._get_operation, …)`, registry instantiation, polymorphic
`operation.apply`). Running graph-vs-raw there would **starve the graph arm** and
measure the closure gap, not material quality. That is an honest limit of a
static call-graph approach without dataflow/points-to analysis.

**Post-closure update (2026-07-30; does not alter the historical ablation
record):** the current source-only JSONPatch graph reaches the scoped
`apply_patch` adequacy contract through static registry and same-file
inherited-member facts. The member rule is intentionally narrow: it follows
only an effective base declaration that the subclass does not override, and it
does not turn inheritance into a call edge or claim general points-to analysis.
No Rust port or new graph-vs-raw experiment follows from this maintenance
measurement; the closed-series conclusion above is unchanged.

### 2.5 Why there is no v4

Decided explicitly in [PHASE7_ABLATION.md](PHASE7_ABLATION.md) (“Series closed”):

- Four attempts already answer the question **as posed**.  
- Searching further for a target “where the graph finally wins” would become
  result-hunting, not research.  
- A true next experiment would be a **different protocol** (hard material budget,
  tool-call / wall budgets, isolation so raw cannot pull the repo iteratively) –
  pre-registered on its own terms – not another small slice.
- `charset-normalizer` is **not** drafted into that role-run: it remains a
  stress-test artifact, not a large ablation.

### 2.6 What this section is not

It is not a claim that the ports in §1 failed. Those ports succeeded under
**golden-first + audit + port_eval**. The ablation asked a **different**
question: causal advantage of graph material over raw for a **cold** agent.
That second question got a negative answer for the tested class of targets.

---

## 3. Why the graph is still useful (at the altitude the evidence supports)

If the graph is not a measured accuracy multiplier for cold porting of small
slices, what is it good for? The evidence supports a **discipline** claim, not a
**capability-multiplier** claim.

### 3.1 Auditability and provenance

Every deterministic edge can be traced to a file/span and an extractor. Weak
calls stay in `call_observations` instead of being promoted into high-confidence
`calls`. `audit_call_edges` turns “does this graph look right?” into a
**repeatable number** (pass rate, anomaly list, dangling list, seeded sample).
That is useful whether or not an LLM is in the loop – for human review, CI gates,
and comparing graph versions over time.

### 3.2 Golden-first porting as a process

The common gate order (license → golden from the **oracle language** → clean
graph → port → `port_eval`) gives the eight bounded port reports a comparable
form. Failures show up as failed goldens or failed audits, not as “the model
said it worked.” That process is independent of winning graph-vs-raw ablations.

### 3.3 Adequacy gating and material audits (ablation methodology)

Even though capability wins did not appear, the ablation harness forced **honest
setup**:

- **Adequacy specs** (`must_reach` / `must_exclude` / closure size) – e.g. isodate
  v3 parser-only: closure 16, 13/13, 0 leaked ([PHASE7_ISODATE_V3_PREREG.md](PHASE7_ISODATE_V3_PREREG.md);
  live adequacy re-check above).  
- **Dry-prep / `audit` / `verify-fill`** – kit isolation, no golden leakage, no
  absolute paths out of the kit, packed == packable(closure). Pre-run review of
  isodate caught real protocol bugs (wrong adequacy roots, `PROVENANCE.md` in
  raw kits, path leakage, underspecified normalization) **before** any N=3 result
  was published ([PHASE7_ISODATE_V3_PREREG.md](PHASE7_ISODATE_V3_PREREG.md)
  “Pre-run corrections”).  
- **Recorded runs + `report`** – scores re-derivable; self-reported efficiency
  columns labelled as such (so a session-truncated self-report cannot be silently
  mixed with harness scores).

That is the scientific hygiene of the project: it can **refuse** a confounded run
and **stop** a failed hypothesis.

### 3.4 Concrete general improvements the gates forced

These are recorded as methodology wins in [PHASE7_ABLATION.md](PHASE7_ABLATION.md)
and target provenance – they apply beyond the ablation that discovered them:

| Improvement | Forced by | What it does |
|---|---|---|
| **`uses_data` / `data_dependencies` packing** | v1 sqlparse under-pack (keyword tables) | Module-level data tables ride with the symbols that read them |
| **Aliased-import + data→reference edges** | humanize adequacy mini-gate | Closure reaches gettext helpers and map data that imports alone missed |
| **Constructor / operator edges** | isodate mini-gate (`Duration.__init__` / `__sub__`) | Negative duration paths reachable without packing whole classes as broad spans |
| **Closure-scoped pack neighbors** | humanize / ablation prep | Graph arm does not see out-of-slice callers through shared helpers |
| **Import-aware semantic-suspicion audit** | sqlparse scale | Legitimate `module.func()` shapes stop looking like false object-call edges |

Each of these made the **graph more honest** or the **pack more complete**. That
is a different success metric from “cold agent scores higher.”

### 3.5 Context assembly for humans and tools

Closure-scoped packs are a **focused view** of a slice: entry symbol, callees,
data deps, snippets, weak observations. On v1’s largest multi-file target, the
recorded run used less material and fewer tools for near-parity scores, but the
corrected protocol did not establish an efficiency win
([PHASE7_ABLATION.md](PHASE7_ABLATION.md) v1 / corrected-v1). On smaller targets
the same focus did not beat raw. Useful product interpretation: the graph is a
**retrieval and scoping tool** for ports and reviews – not a measured accuracy
or efficiency boost when the whole package already fits in context.

### 3.6 What remains a fair, non-inflated claim

**Fair to claim:**

- Deterministic graphs + goldens + `port_eval` have produced **repeatable,
  high-fidelity results within declared port contracts** (Python and bounded C)
  with recorded zero-manual-fix runs.
- Graph audits and ablation adequacy/material checks catch **real defects**
  before numbers ship.  
- The same rails produced **general extractor/packer improvements**.  
- A product CLI (`graphrag-code` / `python -m graphrag_code`) makes the rails
  operable as one surface without changing underlying script behavior.

**Not fair to claim (on current evidence):**

- That graph context measurably beats raw source for cold agents on clean
  benchmark slices.  
- That this work reproduces the Microsoft talk demo or internal infrastructure.  
- That a calls-only static graph is sufficient for dynamic-dispatch-heavy
  architectures. JSONPatch required explicit static registry and same-file MRO
  facts for its scoped closure; that does not demonstrate general dynamic
  dispatch or points-to completeness.
- That the approach is production-ready for million-line C/C++ migration.

### 3.7 If you only remember three sentences

1. **The rails produce bounded evidence:** deterministic extraction, audited
   edges, golden-first ports, and `port_eval` produced a string of verified
   port reports with 0 recorded manual fixes.
2. **The headline hypothesis failed (for this class):** four ablations did not
   show a graph accuracy (or, after v1, efficiency) win over raw source.  
3. **The graph is still worth building** as auditability, provenance, adequacy
   gating, and focused context assembly – especially when you refuse to ship a
   confounded number.

---

## Appendix A – Live checks performed for this write-up

| Check | Command | Result |
|---|---|---|
| Full examples suite | `PYTHONPATH=. uv run pytest examples -q` | **538 passed, 2 xfailed** |
| sqlparse graph audit | `uv run python scripts/audit_call_edges.py --graph byog_sqlparse` | pass rate **1.0**, 0/0/0, **230** calls (current index; the frozen Phase 5 baseline snapshot has 229) |
| cJSON graph audit | `uv run python scripts/audit_call_edges.py --graph byog_cjson` | pass rate **1.0**, 0/0/0, **495** calls (full graph incl. mutation runner; library-only is 188) |
| cJSON port_eval | `uv run python scripts/port_eval.py --source examples/cjson --port examples/cjson_rust --graph byog_cjson` | **59** golden cases, `manual_fixes=0`, **OVERALL PASS=True** |
| isodate adequacy (v3 gate) | `uv run python scripts/ablation.py adequacy --graph byog_isodate --spec scripts/ablation_specs/isodate_adequacy.json` | **adequate: true**, closure **16** |

Ablation N=3 scores and efficiency numbers were **not** re-run; they are taken
from [PHASE7_ABLATION.md](PHASE7_ABLATION.md) and the archived v3 artifacts.

## Appendix B – Where quantitative claims live

| Claim family | Primary sources |
|---|---|
| Eight validated ports, gate order, readiness | [Plan.md](Plan.md) |
| Five Python ports table, sqlparse scale metrics | [PHASE5_REPORT.md](PHASE5_REPORT.md) |
| Ablation scores, medians, series close, no v4 | [PHASE7_ABLATION.md](PHASE7_ABLATION.md) |
| v2/v3 prereg (adequacy criteria, frozen scope) | [PHASE7_HUMANIZE_V2_PREREG.md](PHASE7_HUMANIZE_V2_PREREG.md), [PHASE7_ISODATE_V3_PREREG.md](PHASE7_ISODATE_V3_PREREG.md) |
| Per-target graph counts, deferred scope | `examples/*/PROVENANCE.md` |
| Product CLI | [README.md](README.md), `graphrag-code`, `scripts/graphrag_code.py` |
