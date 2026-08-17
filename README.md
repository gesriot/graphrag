# graphrag-code

`graphrag-code` is a reproducible research repository for deterministic code
graphs and bounded Python/C → Rust ports. For each declared target it captures a
source-language behavior contract, rebuilds and audits a graph, assembles context
packs, and checks the Rust port against that contract. The goal is auditable
evidence, not a claim that a graph makes a model intrinsically better at coding.

## What this demonstrates – and what it does not

The declared port profiles show that this process can produce repeatable,
high-fidelity results **within explicit target contracts**. The evidence is
golden-first: behavior comes from the source-language oracle, not from a
hand-written Rust expectation. Fresh graph audits, context packs, source
contracts, Rust checks, and `port_eval` make each result inspectable.

The closed, pre-registered graph-vs-raw ablation series found **no measured
accuracy or efficiency advantage** for the graph arm on its tested benchmark
class. The graph is therefore supported here as a discipline for auditability,
provenance, adequacy gates, and focused context assembly – **not** as a measured
accuracy multiplier or a demonstrated general porting advantage. It also does
not claim full upstream-library parity, complete static semantics, or a full C
ABI migration beyond each target's stated boundary.

## Product CLI

The installable console command is `graphrag-code`. From this repository:

```bash
uv sync
uv run graphrag-code --help
uv run python -m graphrag_code --help
```

A wheel/sdist build is `uv build`. Installing that wheel provides the same
command without the checkout. Relative `--graph` / `--package` / `--output`
paths are resolved from the invoking working directory, not from a guessed
repository root. The wheel ships the indexer, query, context-pack, and
persisted-integrity doctor modules. It does **not** bundle published `byog_*`
graphs, `examples/`, or experimental evidence.

These generic installed commands operate on user-supplied directories:

- `graphrag-code doctor`
- `graphrag-code query-symbol` / `callers` / `callees`
- `graphrag-code context-pack`
- `graphrag-code index-python` / `index-c`
- `graphrag-code adopt-publication-lock --graph <root> --indexer auto --offline-confirmed`
- `graphrag-code snapshot-history --graph <root>`
- `graphrag-code snapshot-diff --graph <root> --from <id|current> --to <id|current>`
- `graphrag-code snapshot-activate --graph <root> --snapshot <id> --expected-current <id> --activate-confirmed`
- `graphrag-code snapshot-pins --graph <root>`
- `graphrag-code snapshot-pin <id> --graph <root> --expected-registry-revision <token> --pin-confirmed`
- `graphrag-code snapshot-unpin <id> --graph <root> --expected-registry-revision <token> --unpin-confirmed`
- `graphrag-code snapshot-retention-plan --graph <root> --keep-last <N>`
- `graphrag-code snapshot-prune --graph <root> --keep-last <N> --expected-plan-revision sha256:<hex> --prune-confirmed`
- `graphrag-code snapshot-staging --graph <root>`
- `graphrag-code snapshot-staging-cleanup-plan --graph <root>`
- `graphrag-code snapshot-staging-cleanup --graph <root> --expected-plan-revision sha256:<hex> --cleanup-confirmed`
- `graphrag-code snapshot-maintenance-plan --graph <root> --keep-last <N>`
- `graphrag-code snapshot-maintenance-apply --graph <root> --keep-last <N> --expected-maintenance-revision sha256:<hex> --maintenance-confirmed`
- `graphrag-code snapshot-maintenance-reconcile --graph <root> --plan-file <plan.json> [--apply-result-file <result.json>]`
- `graphrag-code snapshot-export-plan --graph <root> --snapshot <id|current>`
- `graphrag-code snapshot-export-apply --graph <root> --snapshot <id|current> --destination <new-dir> --expected-export-revision sha256:<hex> --export-confirmed`
- `graphrag-code mcp --graph <root> --indexer auto`

`graphrag-code mcp` is a local stdio MCP adapter over one existing graph.
It needs no network, no LLM, and no HTTP port. Relative `--graph` is
resolved from the invoking working directory. The process does not infer
a checkout root. stdout is MCP protocol traffic only; diagnostics go to
stderr.

The server exposes a fixed read-only tool set: `graph_status`,
`graph_doctor`, `query_symbol`, `callers`, `callees`, `neighbors`,
`impact`, `type_closure`, `context_pack`, `snapshot_history`, and
`snapshot_diff`. There is no `snapshot_activate`, `snapshot_pin`,
`snapshot_unpin`, `snapshot_retention_plan`, `snapshot_prune`,
`snapshot_staging`, `snapshot_staging_cleanup_plan`,
`snapshot_staging_cleanup`, `snapshot_maintenance_plan`,
`snapshot_maintenance_apply`,
`snapshot_maintenance_reconcile`,
`snapshot_export_plan`, or
`snapshot_export_apply` tool:
activating a retained snapshot, writing operator retention pins,
planning keep-last retention, pruning CAS-verified candidates,
listing staging directories, emitting a read-only staging cleanup
plan, applying that plan, composing both maintenance plans,
applying the composite plan, reconciling a saved plan against
the live graph, planning a snapshot export, or applying that
export is an
explicit CLI operation and is intentionally absent from MCP. Tool arguments cannot select another
graph. There is no indexing, publishing, retention, port-eval,
compiler/Clang, SQL, or shell tool. Snapshot history is a bounded local
listing of retained published ids. Snapshot diff is structural
persisted-row comparison, not semantic equivalence: a modified row means
canonical persisted fields differ. Staging directories are notices, not
history. Neither history nor diff accepts graph paths, filesystem paths,
output paths, or table names.

Query, context-pack, doctor, and status tools accept an optional
`snapshot` argument (`current` by default, or one explicit published
id). Historical reads do not require `snapshot-activate` and do not
change `current`. Each call takes a shared advisory lock on the
graph-root `.publish.lock`, pins the selected retained snapshot for the
rest of the call, and reports that canonical id in the response
envelope. `current` is resolved exactly once when it is selected; an
explicit id does not read `current`. Cooperating publishers and
keep-last retention wait until the call releases the lock. The MCP tool
set remains exactly 11 read-only tools. `snapshot_history` and
`snapshot_diff` keep their own reference contracts. A managed graph without that regular lock file is rejected during
MCP startup. MCP never creates the lock, and neither does the doctor or
`ByogGraph`. To add the protocol to an existing pre-lock managed graph
without reindexing, run `graphrag-code adopt-publication-lock` after an
offline confirmation. This is not a distributed lease service and does
not protect against tools that ignore `.publish.lock`. Manual deletion
or corruption still returns a controlled error. This is agent access to
a local graph, not a UI, HTTP service, or semantic search backend.

`adopt-publication-lock` is an explicit migration, never an automatic
MCP or doctor side effect. `--offline-confirmed` is required to create
`.publish.lock`. Passing it asserts that no legacy reader or
publisher/retention process that ignores `.publish.lock` is active, and
that future publishers will use the current lock-aware protocol. The
program cannot prove those conditions: processes that never open the
lock file are invisible. Creating the file while such a process is live
would split the locking domain. Without the flag, a managed graph
missing the lock exits 2 and is not modified. The command may create
only `<graph>/.publish.lock`. It does not reindex, extract, compile,
publish, retain, alter `current`, or rewrite manifests, parquet, or
settings. Immutable checked-in `byog_*` evidence stays on the
explicitly unleased compatibility path until an operator adopts a
disposable or operational copy. The JSON `payload_unchanged` field compares
the doctor's persisted-input fingerprints before and after adoption (excluding
the lock itself); it can be `false` if another actor changes a valid snapshot
during the command, even though adoption code writes only the lock.

Tool schemas reject unknown fields. List and traversal sizes, context-pack
text/type evidence, doctor samples, and the serialized response envelope all
have hard limits; the MCP context-pack tool deliberately does not expose the
unbounded CLI `--full-text` mode.

`snapshot-history` and `snapshot-diff` are read-only. They hold one
shared `.publish.lock` lease while resolving `current` once, listing
retained snapshots, loading compared snapshots, and building the
response. Cooperating publishers wait. The commands are strict by
default: a managed graph without `.publish.lock` exits 2 and is not
modified; they never create the lock and point at
`adopt-publication-lock`. `--allow-unlocked-legacy` is an explicit
CLI-only compatibility path for immutable pre-lock evidence. It still
fingerprints inputs and reports that there is no retention guarantee.
MCP never exposes that option. History is newest-first on canonical
published ids, excludes `.staging-*`, and treats unexpected snapshot
entries as unsafe. Staging notices retain the exact count and return at
most 20 UTF-8-byte-sorted names with explicit `returned` / `truncated`
fields. Diff compares `entities`, `relationships`,
`text_units`, and optional `call_observations` by the nonempty string
`id` column. Samples are truncated independently per
added/removed/modified category; totals stay exact. A missing field
differs from explicit null, and JSON booleans differ from numbers. This
is not natural-language search, a UI, an HTTP service, repair, or reindex.

`snapshot-activate` is an explicit mutating CLI operation. It changes
only `<graph>/current` so an operator can activate an already-published
retained snapshot. `--activate-confirmed` and `--expected-current` are
mandatory. Without confirmation the command exits 2, prints a controlled
diagnostic to stderr, writes nothing to stdout, and changes nothing.
`--expected-current` is a compare-and-swap guard: while one exclusive
existing-lock lease is held, `current` is resolved exactly once and the
pointer is written only when that id still matches. `--snapshot` and
`--expected-current` must be explicit canonical published ids, not
`current`, paths, traversal, staging ids, or aliases. Activating the
already-current snapshot is a successful idempotent no-op with
`changed=false`. The command requires an already-adopted regular
`.publish.lock`; a missing lock exits 2, points at
`adopt-publication-lock`, and never creates the lock. It does not
delete, retain, publish, repair, or reindex. Both backward and forward
activation among retained published ids are allowed; existing retention
continues to protect whichever snapshot is current. Advisory locks
protect only cooperating processes. This command is intentionally
absent from MCP.

`snapshot-pins`, `snapshot-pin`, and `snapshot-unpin` are operator-managed
retention metadata. They read or write only `<graph>/.snapshot-pins.json`.
They do not activate a snapshot, change `current`, publish, reindex, back
up, or replicate the graph. Listing is read-only and never creates the
registry; an absent file is an empty operator pin set with revision
`absent`. Pin and unpin require `--pin-confirmed` / `--unpin-confirmed`
and `--expected-registry-revision` (`absent` or `sha256:<hex>` of the
exact file bytes). A stale revision exits 1 and changes nothing.
Pinning an already-pinned id or unpinning an absent id is a successful
idempotent no-op. Unpinning the last pin writes the canonical empty
registry and does not unlink it. Unpin performs no immediate deletion;
the snapshot only becomes eligible for a later cooperating keep-last
operation. Cooperating keep-last cleanup protects `current`, existing
doc-claim/frozen-evidence pins, and these operator pins. A malformed
registry aborts publication and cleanup before `current` changes or
snapshots are deleted. All three commands require an already-adopted
regular `.publish.lock` and never create that file. Listing holds one
shared lease; pin and unpin hold one exclusive lease. Advisory locks
protect only cooperating processes. Manual or lock-ignoring deletion can
still remove a pinned snapshot. These commands are intentionally absent
from MCP. The MCP tool set remains exactly 11 read-only tools.

`snapshot-retention-plan --graph <root> --keep-last <N>` is a read-only
report of what cooperating keep-last cleanup would retain and delete. It
shares the same selection helper as cleanup. `keep_last` has an
effective minimum of 1. Current and every existing claim or operator pin
are retained even when that protected set exceeds `keep_last`; newest
remaining published snapshots fill the floor. Staging directories are
notices, not candidates. Dangling pins are reported and are not invented
as retained snapshots. An absent `.snapshot-pins.json` is an empty
operator pin set and is not created. The command requires an
already-adopted regular `.publish.lock`, holds one shared lease for the
complete response, and never creates that lock. It does not prune,
apply, delete, activate, publish, or change any graph file.
`plan_revision` is `sha256:<hex>` over the canonical decision inputs,
schema version, and exact retained/deletion result. `snapshot-prune`
consumes that token as a compare-and-swap guard. Advisory
locks protect only cooperating processes. This command is intentionally
absent from MCP. The planner rechecks `current`, the
published/staging listing, claim pins, exact registry revision, and lock
identity before returning; detected lock-ignoring mutation exits 1.
Cleanup fails before deletion when `current` is missing/dangling or a
`snapshots/` entry is unsafe. Publication performs that validation before
promotion and skips deletion if an input becomes ambiguous only after the
new `current` has been written.

`snapshot-prune --graph <root> --keep-last <N> --expected-plan-revision
sha256:<hex> --prune-confirmed` is the explicit mutating CLI that applies
exactly one recomputed retention plan. `--prune-confirmed` and
`--expected-plan-revision` are mandatory. There is no dry-run: the plan
command is the preview. Without confirmation the command exits 2 and
changes nothing. Whitespace, uppercase hex, or any token other than
`sha256:<64 lowercase hex>` is rejected before the lease. One exclusive
existing-lock lease recomputes the shared planner, requires the observed
`plan_revision` to match, then deletes only those
`deletion_candidates`. A stale revision, keep-last mismatch, or changed
current/pin/published set exits 1 and changes nothing. Current, existing
operator/claim pins, staging directories, and dangling pins are never
deleted. Recursive deletion is not transactionally atomic: a later-
candidate failure or process crash can leave a partial prune, reported
with `ok=false`, `partial=true`, `deleted_snapshots`, `failed_snapshot`,
`not_attempted_snapshots`, `filesystem_may_have_changed=true`, and
`retry_requires_fresh_plan=true`. `changed` counts only snapshot
directories whose complete removal succeeded; a failing recursive
removal can still change files before it raises. There is no rollback,
trash, or recovery protocol. The command never creates
`.publish.lock` or `.snapshot-pins.json` and is intentionally absent
from MCP. Advisory locks protect only cooperating processes.

`snapshot-staging --graph <root>` is a read-only structural inventory of
direct `snapshots/.staging-*` entries. It requires a managed
`current + snapshots/` graph and an already-adopted regular
`.publish.lock`, holds one shared existing-lock lease across both
discovery scans and the complete response, and never creates that lock
or changes `current`, `.snapshot-pins.json`, published payloads, or
staging entries. Publishers construct `.staging-*` without holding the
exclusive graph-root publication lock and acquire it exclusively only
for promotion, so
the shared graph lease is not a liveness lease over a staging writer.
Cooperating publishers also hold a dedicated advisory writer lease on
`snapshots/.staging-<id>/.staging-writer.lock` for the staging-write
interval. In an already-managed graph, reacquiring existing writer-lock
metadata briefly joins the shared `.publish.lock` domain, then releases
that graph gate before payload construction; reacquisition is
nonblocking while gated, and fresh publisher lock creation is not gated.
This prevents cleanup from removing a lock while
a cooperative writer is waiting for it without serializing ordinary
staging writes. Inventory observes that private lease with a nonblocking
exclusive probe. Contended acquisition means only that a cooperating
process held the writer lease at that scan. A successful
acquire-and-release means only that the lease was not held at that
instant. Neither proves writer death, ownership, or cleanup eligibility.
Missing writer-lock metadata is `legacy_absent` / `unverifiable`,
including the small directory-creation-to-lock window. The persistent
lock file is protocol metadata, not proof of ownership. Two-scan
agreement binds writer-lock identity and lease state; a
held/not-held transition or lock appearance, disappearance, replacement,
or type change exits 1 with empty stdout. Schema version is 2 because
each staging entry now reports `writer_lease_protocol`,
`writer_lease_state`, `writer_lock_present`, and `writer_lock_regular`.
Ownership is always `unknown`. No wall-clock age, mtime heuristic, PID
probe, host identity, or guessed timeout is used to infer ownership.
This inventory does not apply cleanup; `cleanup_eligible` is always false.
`complete_payload_candidate` means only that the expected top-level file
names are present, not parquet validity, manifest integrity, successful
publication, or deletion safety. `staging_revision` is an informational
`sha256:<hex>` over the schema version, current published id, published
snapshot ids, and the complete reported inventory. This command does not
accept or apply that token. A graph with no staging entries is a valid
exit-0 report with `staging_count=0`. The command is intentionally
absent from MCP.

`snapshot-staging-cleanup-plan --graph <root>` is a separate read-only
CLI plan over that same schema-2 inventory. It reuses the inventory
scanner under one shared existing-lock lease, never creates
`.publish.lock` or `.snapshot-pins.json`, and never mutates staging,
payloads, or writer-lock bytes. Cleanup-plan schema 1 was
read-only/pre-apply (`apply_supported=false`). Schema version is now 2
and `apply_supported` is true because `snapshot-staging-cleanup` is
the separate CAS apply. `cleanup_applied` stays false. A staging name
appears in `deletion_candidates` only when the stable observation is a
real directory whose suffix is a canonical published id,
`writer_lease_protocol=cooperative_v1`,
`writer_lease_state=not_held_at_scan`, and the writer-lock file is
present and regular. Payload completeness is not a selection
condition. Everything else is a deterministic `blocked_entries` row
with a machine-readable reason (`held_writer_lease`,
`legacy_or_missing_writer_lock`, `noncanonical_staging_name`,
`non_directory_staging_entry`). Unsafe symlinked or non-regular
metadata and two-scan disagreement still exit 1 with empty stdout
instead of becoming ordinary blockers. `deletion_candidates` means
only "candidate in this read-only plan": not writer death, not
ownership, not permission to delete, not a durable lease, and not
inventory `cleanup_eligible`. A future writer may acquire the private
writer lease after the plan is emitted. `staging_state_revision` is
`sha256:<hex>` over the internal two-scan consistency token, including
current/lock identities and staging/writer-lock inodes, so an inode
replacement is visible even when public inventory fields match.
`observed_staging_revision` repeats the inventory `staging_revision`.
`plan_revision` is `sha256:<hex>` over compact canonical JSON binding
`schema_version`, `current`, `published_snapshots`,
`observed_staging_revision`, `staging_state_revision`,
`deletion_candidates`, `blocked_entries`, `ownership_inference`,
`cleanup_applied`, and `apply_supported`. Graph path, counts, notices,
`ok`, and `staging_entries` are excluded. This command does not accept
or apply that token. Schema-1 revisions are not accepted by apply.
`snapshot-staging-cleanup --graph <root> --expected-plan-revision
sha256:<hex> --cleanup-confirmed` is the explicit mutating CLI. There
is no dry-run. Confirmation is required even when the candidate set is
empty. It acquires one exclusive existing-lock graph lease, recomputes
the schema-2 plan, compares `plan_revision`, nonblockingly claims
every selected existing writer lock, revalidates staged-directory and
writer-lock identities plus the bounded top-level structural token,
and only then deletes. The plan's `not_held_at_scan` observation is
not that exclusive claim. Recursive deletion is not transactionally
atomic. A partial result reports `partial=true` and always requires a
fresh plan; there is no rollback, trash, or recovery. Both commands
are intentionally absent from MCP. Staging entries and their top-level
children are each capped at 64, and the reported published-snapshot
list is capped at 4096. Enumeration is descriptor-relative with
`O_NOFOLLOW`; a platform without that safe primitive is rejected
instead of falling back to a pathname traversal. Advisory locks
protect only cooperating processes. No backup, recovery, quarantine,
or distributed lease is claimed.

`snapshot-maintenance-plan --graph <root> --keep-last <N>` is a
read-only composite of those two existing plans. It holds exactly one
shared existing-lock lease, computes the current
`snapshot-retention-plan` and schema-2 `snapshot-staging-cleanup-plan`
inside that lease, and never takes a nested graph lease. The embedded
objects are the exact standalone public plan objects for the same
graph and `keep_last`. Top-level `current` and `published_snapshots`
must agree with both embedded plans. `actionable_components` lists
`snapshot-prune` and/or `snapshot-staging-cleanup` only when the
matching embedded deletion set is non-empty, in fixed UTF-8-byte
order. It does not recommend an apply order: applying either
component can invalidate the other revision. A fresh composite or
standalone plan is required after every apply.
`maintenance_revision` is `sha256:<hex>` over compact canonical JSON
binding `schema_version`, `keep_last`, `current`,
`published_snapshots`, the two embedded `plan_revision` tokens,
`actionable_components`, and `fresh_plan_required_after_any_apply`.
Graph path, counts, notices, `ok`, and presentation-only embedded
fields are excluded. Schema version is 1. The plan command never
creates `.publish.lock` or `.snapshot-pins.json` and does not change
`current`, published snapshots, pins, staging, or writer-lock
metadata. `snapshot-maintenance-apply --keep-last <N>
--expected-maintenance-revision sha256:<hex> --maintenance-confirmed`
is the mutating counterpart: it holds one exclusive existing-lock
lease, recomputes the same composite, compares `maintenance_revision`
before any mutation, claims every selected existing writer lock,
revalidates from the captured consistency tokens without recomputing
the cleanup plan after those claims, and then deletes staging
leftovers before prune candidates. That cleanup-then-prune order is
the apply command's conservative execution order, not a
recommendation on the read-only plan. Confirmation is required even
when both deletion sets are empty. Recursive deletion is not
transactionally atomic. A partial result reports
`partial=true`, `filesystem_may_have_changed=true`, and
`retry_requires_fresh_plan=true`; there is no rollback. Every result
also sets `fresh_plan_required_after_any_apply=true`,
including complete and empty success. Standalone
`snapshot-prune` and `snapshot-staging-cleanup` remain available.
`snapshot-maintenance-reconcile --graph <root> --plan-file
<saved-plan.json> [--apply-result-file <saved-result.json>]` is the
read-only aftermath inspection for that composite. It accepts only
bounded regular files (1 MiB) opened read-only without following
symlinks. Input loading, structural validation, embedded retention and
staging-cleanup self-hash validation, direct candidate-name validation,
and optional result/plan cross-validation all finish before graph
inspection. Apply-result component and candidate outcomes must form the
exact schema-1 ordered partition, and its current, published, and
remaining sets must match the saved plan. The command then holds one
shared existing-lock lease and reports whether planned published and
staging pathnames are still present. `ok`
means the read completed, not that maintenance succeeded. An absent
pathname does not prove deletion cause. There is no recovery,
rollback, or recommended retry. A new plan is still required before
any later mutation. `snapshot-export-plan --graph <root> --snapshot
<id|current>` is a read-only inspection of one retained published
snapshot's direct envelope payload files. It does not create an
archive, copy files, or mutate the graph. The plan is not a backup
and is not authorization to delete anything. `export_revision` is a
self-consistency token for that exact observed payload; a future
export apply must capture a fresh plan.
`snapshot-export-apply --graph <root> --snapshot <id|current>
--destination <new-dir> --expected-export-revision sha256:<hex>
--export-confirmed` recomputes that plan under one shared
existing-lock lease and copies the accepted payload files into a
newly created destination only when the revision still matches. It
does not mutate the graph, overwrite a pre-existing destination, or
create an archive. The copy is not a backup and is not authorization
to delete anything. The composite plan, apply,
reconcile, export-plan, and export-apply
commands are intentionally absent from MCP. The MCP
tool set remains exactly 11 read-only tools.

Query and context-pack commands accept optional
`--snapshot <id|current>`. Omitting it preserves the existing default
current/legacy-flat read. `--snapshot current` explicitly selects the
snapshot named by `current`. An explicit published id reads
`<graph>/snapshots/<id>` without mutating `current`. Historical reads
are read-only: they do not activate, publish, retain, repair, or
reindex. One shared `.publish.lock` lease pins the selected snapshot
against cooperating keep-last retention for the complete response.
Explicit query/context selectors require that existing regular lock and
never create it. Only their omitted-selector CLI compatibility read may
use pre-lock evidence without a lease; that path has no retention guarantee.
Advisory locks do not protect against non-cooperating actors. This is not
natural-language search, a UI, an HTTP service, repair, or reindex.

`index-python`, `index-c`, and `index` accept opt-in `--reuse-unchanged`.
That is content-addressed whole-snapshot reuse, not per-file delta
indexing and not a watcher. When the supported deterministic inputs, the
producer sources/runtime versions, and `source_root` still match a doctor-valid current
snapshot, the command reprints that snapshot and does not extract or
publish. Default indexing without the flag still rebuilds.

Reuse is supported only for host-independent modes: Python
`use_advanced=false`, and C with every compiler/Clang overlay off. Those
unsupported flags still rebuild when `--reuse-unchanged` is omitted. An
explicit `--reuse-unchanged` combined with them exits 2 and does not
pretend a toolchain-complete cache key exists. `manifest.corpus_hash`
stays the existing nullable field; reuse records a separate
`index_input` block. `port-eval` still performs fresh disposable
indexing. CLI indexers take `.index.lock` for the whole index operation
and only take `.publish.lock` inside publication, never during extraction.

`graphrag-code port-eval` is source-checkout only. Gate mode reads
`scripts/port_gates.json` and `examples/`; even an explicit `--graph` /
`--source` / `--port` invocation writes packs under the checkout `output/`
tree and shells the checkout scripts. A wheel install without those
checkout assets exits 2 with a diagnostic. That is not standalone port
evaluation.

Source-checkout script paths remain supported:

```bash
uv run python scripts/graphrag_code.py --help
uv run python scripts/persisted_graph_doctor.py --graph <root> --indexer python
uv run python scripts/adopt_publication_lock.py --graph <root> --indexer python --offline-confirmed
uv run python scripts/snapshot_compare.py history --graph <root>
uv run python scripts/snapshot_compare.py diff --graph <root> --from current --to current
uv run python scripts/snapshot_activate.py --graph <root> --snapshot <id> --expected-current <id> --activate-confirmed
uv run python scripts/snapshot_pins.py --graph <root>
uv run python scripts/snapshot_pins.py pin <id> --graph <root> --expected-registry-revision absent --pin-confirmed
uv run python scripts/snapshot_retention.py --graph <root> --keep-last 2
uv run python scripts/snapshot_prune.py --graph <root> --keep-last 2 --expected-plan-revision sha256:<hex> --prune-confirmed
uv run python scripts/snapshot_staging.py --graph <root>
uv run python scripts/snapshot_staging_cleanup_plan.py --graph <root>
uv run python scripts/snapshot_staging_cleanup.py --graph <root> --expected-plan-revision sha256:<hex> --cleanup-confirmed
uv run python scripts/index_python.py --package <pkg> --graph <out>
uv run python scripts/index_c.py --package <pkg> --graph <out>
uv run python scripts/graph_query.py symbol <title> --graph <root>
uv run python scripts/context_pack.py <title> --graph <root>
uv run python scripts/port_eval.py --all-gates --full
```

This is not a PyPI publication, a production service, or a claim of semantic
equivalence.

## Verify the repository

From the repository root, run the portable full-evidence gate:

```bash
uv run python scripts/port_eval.py --all-gates --full
# Context packs are fail-closed evidence: every requested pack must generate.
# Optional C profile type_context (inih/cJSON) indexes disposable graphs with
# --clang-type-uses and validates type_*_closure; never rewrites byog_*.
```

It rebuilds disposable graphs under `output/port_gates/`, runs each declared
source contract and Rust port evaluation, and reports named source-only gaps.
`--full` additionally health-checks each mutable published local graph against
the current extractor without rewriting it. Some live diagnostic and
frozen-record checks deliberately need local graph evidence; the durability
inventory names each one and whether absence is a failure or an explicit skip.
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
| The shared oracle discipline and live residuals | [docs/ORACLE_CONTRACT.md](docs/ORACLE_CONTRACT.md) | The independent-oracle rules, their regression scars, the compliance review, and one combined command. |
| Whether a graph-backed claim survives a clean checkout | [docs/EVIDENCE_DURABILITY.md](docs/EVIDENCE_DURABILITY.md) | The manifest-derived split between disposable gate output, local frozen snapshots, published oracle inputs, and historical records. |
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
| [`jsonpatch`](examples/jsonpatch/PROVENANCE.md) | An RFC 6902 source-only extractor boundary case. Its Python apply contract, dynamic-dispatch diagnostics, runtime registry oracle, and a current adequate apply-slice closure are evidence. The closure uses narrow static registry and same-file inherited-member facts; no Rust port or new ablation outcome is claimed. |
| [`humanize`](examples/humanize/PROVENANCE.md) | A source-only number-formatting slice. Python goldens and an adequacy-clean graph support the closed ablation record, which found no graph advantage; there is no Rust port or Rust-port claim. |
| [`isodate`](examples/isodate/PROVENANCE.md) | A source-only duration parsing/formatting slice used by the closed ablation record. Its protected archive remains evidence of the experiment; there is no Rust port, and the aggregate gate deliberately does not regenerate the frozen archive. |

The gate also carries focused diagnostics rather than treating them as silent
implementation details: C preprocessor liveness is checked against compiler
output, Python registry extraction is checked against imported runtime objects,
runtime call observations are compared with published graph calls, and cJSON's
public surface/refusals are checked against its real header, C traces, and
compiler locations. Their scope and residuals are recorded in the target
provenance files and the [evidence audit](docs/EVIDENCE_AUDIT_20260728.md).

**Optional C compiler overlays (each default off):** independently selectable
flags on `scripts/index_c.py`:

- `--compiler-dependencies` – flattened TU `depends_on` edges
  (`fact_kind=translation_unit_dependency`) via per-entry `compiler -M` and
  package-path filtering (may be transitive).
- `--compiler-includes` – direct `includes` edges
  (`fact_kind=configured_direct_include`) via per-entry `compiler -E -H`
  hierarchy reconstruction (parent → only its direct includes).
- `--clang-signatures` – attach configured Clang `qualType` / storage metadata
  to **existing** tree-sitter function entities from the AST audit’s
  unambiguously matched, line-confirmed rows only (no new entities/edges).
- `--clang-calls` – attach configured Clang direct-call evidence metadata to
  **existing** tree-sitter `calls` relationships from the AST call-site audit’s
  `matched_internal` rows only (exact span + byte-offset attachment; no new
  entities/edges; base `confidence`/`extractor` unchanged).
- `--clang-types` – attach configured Clang type-declaration evidence metadata
  to **existing** tree-sitter `struct` / `enum` / `typedef` entities from the
  AST type-declaration audit’s matched rows only (exact `tree_sitter_title` +
  entity type + `symbol_name` + path + canonical graph span; no new entities,
  no alternate-site entities).
- `--clang-type-uses` – publish aggregated `uses_type` relationships from the
  type-use audit’s `matched_internal` rows only (one edge per owner/target
  entity-id pair; recursive self-edges allowed). Observation counts and edge
  counts differ; fail-closed residuals abort. Default off. When present, query
  with `graph_query.py types-used-by` / `type-users` / `type-closure` (or
  `graphrag_code.py` equivalents). Direct queries list one-hop titles;
  `type-closure` is a bounded cycle-safe BFS over **only** `uses_type`
  (`--direction dependencies|users|both`, `--max-depth` / `--max-nodes` /
  `--max-edges`; caps truncate returned material while totals within depth
  stay exact; malformed rows fail closed). Context packs surface bounded direct
  `type_dependencies` / `type_dependency_edges` / `type_user_edges` from the
  full relationship set (not the 30-neighbor cap). Default `--type-depth 1`
  keeps pack JSON unchanged; depth > 1 adds `type_dependency_closure` /
  `type_user_closure` with min depths and the same observation/text bounds.
  Missing or non-unique closure endpoints remain explicit node payloads so
  returned counts and truncation flags stay honest.
- `--clang-type-shapes` – attach configured Clang **ordered direct member-name**
  evidence to **existing** tree-sitter `struct` / `enum` entities from the
  type-shape audit’s `matched_shape` rows only (`clang_shape_*` namespace;
  exact `tree_sitter_title` + entity type + `symbol_name` + package-relative
  path + canonical graph span). Hard equality is the ordered member-name list
  and nothing else: `qualType`, `desugaredQualType`, enum values, bit-field
  widths, and member locations are published as configuration-relative
  diagnostic evidence, **not** ABI, layout, size, alignment, offset, FFI-safety,
  or Rust `repr` claims. Residual buckets (`tree_sitter_only_members`,
  `clang_only_members`, `member_order_mismatch`,
  `duplicate_or_ambiguous_members`, `macro_location_unsupported`,
  `owner_unmatched`) fail closed; `unsupported_member_form` and
  `outside_package_declarations` stay observation-only and are counted in the
  manifest. No new entities, relationships, `uses_type` edges, or alternate-site
  entities. Default off.

When any non-empty combination of `--clang-signatures`, `--clang-calls`,
`--clang-types`, `--clang-type-uses`, and `--clang-type-shapes` is enabled,
`index_c` builds **one shared in-memory AST capture** (one `-ast-dump=json` per
`compile_commands.json` entry) and every enabled overlay consumes it. The
type-declaration audit is likewise built at most once and reused by the
type-use and type-shape builders. There is **no** persistent AST cache and AST
JSON is never written to manifests or parquet. Enabling any non-empty subset
still dumps once per entry (N dumps for N entries, never 2N–5N); none of the
flags dumps nothing. Trust/confidence boundaries are independent per overlay
manifest block.

These are narrow configuration-derived layers on top of tree-sitter-c – not
full type resolution, ABI verification, multi-config coverage, points-to
analysis, type-use / `uses_type` proof, macro-complete call proof, or
production C/C++ completeness. `-M` / `-H` / AST-dump are GNU/Clang-specific
adapters, not a universal compiler API. Wrappers, response files, `--config`,
modules, plugins, and PCH fail explicitly. See
[docs/graph_schema.md](docs/graph_schema.md).

**Clang AST audits (standalone diagnostics):**

- `scripts/c_clang_ast_audit.py` – function definitions / signatures vs
  tree-sitter entities. Publishing selected matched signature *fields* into
  BYOG requires the separate explicit `--clang-signatures` flag.
- `scripts/c_clang_signature_graph_audit.py` – **read-only integrity audit**
  for already-persisted `clang_signature_*` / `clang_qual_type` entity fields
  and the `clang_signatures` manifest block. Does not invoke Clang, read
  `compile_commands.json`, build an AST capture, reindex, publish, or rewrite
  graphs, and never repairs data. `--graph` (plus optional `--snapshot` /
  `--output`) resolves the snapshot, SHA-256-fingerprints every graph input
  before and after the run, and reports that read-only verification
  alongside the findings. `--output` is refused inside the audited graph
  root so report generation cannot invalidate that guarantee. The
  deterministic JSON exposes `state`, `classification`, `violations`,
  counts, provenance and limitations. Exit 0 = valid (including
  `legacy_absent` and `off` with zero signature fields), 1 = integrity
  violations, 2 = unreadable graph/snapshot/manifest. A missing manifest
  block never legitimizes existing signature fields, a present
  `clang_signatures` key that is null/list/string is invalid (not
  legacy), `mode=off` with signature fields is an error, and an enabled
  block with partial, corrupted, or extra fields is an error. Shared
  producer-contract helpers live in `c_clang_signatures.py`
  (`validate_persisted_signature_overlay`). Published C graph health runs
  the same pure check without changing extractor comparison.
- `scripts/c_clang_call_audit.py` – call sites vs tree-sitter `calls` edges
  (direct internal matches; external/indirect remain observations). Call-site
  matching is byte-offset-first with strict line/column fallback and complete
  tree-sitter edge accounting. Publishing selected matched call *evidence
  fields* into BYOG requires the separate explicit `--clang-calls` flag.
- `scripts/c_clang_call_graph_audit.py` – **read-only integrity audit** for
  already-persisted `clang_call_*` relationship fields and the `clang_calls`
  manifest block. Does not invoke Clang, read `compile_commands.json`, read C
  sources, reconstruct byte offsets, reindex, publish, or rewrite graphs, and
  never repairs data. `--graph` (plus optional `--snapshot` / `--output`)
  SHA-256-fingerprints every graph input before and after the run. `--output`
  is refused inside the audited graph root. Exit 0 = valid (`legacy_absent` /
  `off` / enabled), 1 = integrity violations, 2 = unreadable
  graph/snapshot/manifest. Shared producer-contract helpers live in
  `c_clang_calls.py` (`validate_persisted_call_overlay`). Published C graph
  health attaches the same check as `clang_call_integrity`.
- `scripts/c_compiler_dependency_graph_audit.py` – **read-only integrity audit**
  for already-persisted compiler TU `depends_on` /
  `translation_unit_dependency` relationships and the
  `compiler_dependencies` manifest block. Does not invoke a compiler, read
  `compile_commands.json`, read C/header sources, run `compiler -M`,
  reconstruct dependency sets, reindex, publish, or rewrite graphs, and never
  repairs data. `--graph` (plus optional `--snapshot` / `--output`)
  SHA-256-fingerprints every graph input before and after the run. `--output`
  is refused inside the audited graph root. Exit 0 = valid (`legacy_absent` /
  exact `off` / enabled `mode=compiler_m`), 1 = integrity violations, 2 =
  unreadable graph/snapshot/manifest. Shared producer-contract helpers live
  in `c_compiler_facts.py`
  (`validate_persisted_compiler_dependency_overlay`). Published C graph
  health attaches the same check as `compiler_dependency_integrity`. Non-C
  graphs do not gain this C-only failure.
- `scripts/c_compiler_include_graph_audit.py` – **read-only integrity audit**
  for already-persisted compiler direct `includes` /
  `configured_direct_include` relationships and the `compiler_includes`
  manifest block. Does not invoke a compiler, read `compile_commands.json`,
  read C/header sources, run `compiler -E -H`, reconstruct include
  hierarchies, reindex, publish, or rewrite graphs, and never repairs data.
  `--graph` (plus optional `--snapshot` / `--output`) SHA-256-fingerprints
  every graph input before and after the run. `--output` is refused inside
  the audited graph root. Exit 0 = valid (`legacy_absent` / exact `off` /
  enabled `mode=compiler_eh`), 1 = integrity violations, 2 = unreadable
  graph/snapshot/manifest. Shared producer-contract helpers live in
  `c_compiler_includes.py`
  (`validate_persisted_compiler_include_overlay`). Published C graph health
  attaches the same check as `compiler_include_integrity`. Non-C graphs do
  not gain this C-only failure. The dependency overlay remains a separate
  layer.
- `scripts/c_preprocessor_liveness_graph_audit.py` – **read-only integrity
  audit** for already-persisted C preprocessor-liveness row stamps and the
  `preprocessor_liveness` manifest block. Does not invoke a compiler, read
  `compile_commands.json`, read C/header sources, reconstruct macro tables
  or branch decisions, compare the recorded digest with the current host,
  reindex, restamp, publish, or rewrite graphs, and never repairs data.
  `--graph` (plus optional `--snapshot` / `--output`) SHA-256-fingerprints
  every graph input, including optional `call_observations.parquet`, before
  and after the run. `--output` is refused inside the audited graph root.
  Exit 0 = valid (`legacy_absent` / `no_compiler` / `compiler_builtins`),
  1 = integrity violations, 2 = unreadable graph/snapshot/manifest. Shared
  producer-contract helpers live in `c_preprocessor.py`
  (`validate_persisted_preprocessor_liveness`). Published C graph health
  attaches the same check as `preprocessor_liveness_integrity`. Non-C
  graphs do not gain this C-only failure. This checks persisted internal
  consistency only, not source-correctness of recorded liveness.
- `scripts/c_overlay_coherence_graph_audit.py` – **read-only snapshot-wide
  coherence audit** for the seven compiler-backed overlays
  (`compiler_dependencies`, `compiler_includes`, `clang_signatures`,
  `clang_calls`, `clang_types`, `clang_type_uses`, `clang_type_shapes`).
  Reuses the existing component validators and additionally requires every
  enabled subset to share one `compile_commands_digest`, `n_compile_entries`,
  and normalized compiler census. Off/legacy blocks are ignored;
  `preprocessor_liveness` remains an aggregate integrity component but its
  provenance is reported independently and is never compared with compiler
  overlay identities.
  Does not invoke a compiler, read sources or `compile_commands.json`,
  reconstruct overlay facts, or rewrite graphs. Exit 0 = valid
  (`legacy_absent` / `off` / `coherent`), 1 = integrity or coherence
  violations, 2 = unreadable graph/snapshot/manifest. Published C graph
  health attaches `c_overlay_coherence_integrity`. Non-C graphs do not gain
  this C-only failure.
- `scripts/byog_snapshot_graph_audit.py` – **read-only language-independent
  snapshot-envelope audit** for the persisted BYOG directory, core
  `manifest.json` fields, and parquet census written by
  `publish_byog_snapshot()`. Applies to every BYOG indexer, not only C.
  Does not invoke an extractor, compiler, or Clang; read source packages or
  `compile_commands.json`; reconstruct overlays; reindex, repair, publish, or
  rewrite graphs; or compare `source_root`, `git_commit`, or `created_at`
  with the current host. `--graph` (plus optional `--snapshot` / `--output`)
  SHA-256-fingerprints every regular file in the selected snapshot
  (including optional `settings.yaml`), the `current` pointer, and the
  snapshots-directory listing. `--output` is refused inside the audited
  graph root, including through symlink aliases. Exit 0 = valid envelope,
  1 = integrity violations, 2 = unsafe path / malformed JSON / unreadable
  parquet / missing required input. Shared producer-contract helpers live in
  `byog_snapshot_integrity.py` (`validate_persisted_byog_snapshot`).
  Published graph health attaches `snapshot_integrity` for every non-frozen
  graph and short-circuits a broken envelope before fresh extraction or
  language-specific overlay checks.
  `publish_byog_snapshot()` builds payload files in a private
  `snapshots/.staging-<id>/` directory. Immediately after creating that
  directory it creates `snapshots/.staging-<id>/.staging-writer.lock`
  and holds an exclusive advisory writer lease for the complete
  staging-write interval, including the wait for the graph-root
  exclusive `.publish.lock`. The graph-root lock is held exclusively only for
  removing that writer-lock metadata, the atomic staging-to-final
  rename, the `current` pointer update, and keep-last retention. The
  published snapshot and `manifest.files` never contain the writer-lock
  file. Reacquiring existing writer-lock metadata in an already-managed
  graph uses a short shared graph-lock gate which ends before payload
  construction; reacquisition is nonblocking while gated, and fresh
  publisher lock creation is not gated. Concurrent
  staging writes therefore remain concurrent while cleanup cannot race a
  waiting cooperative writer during lock removal. Staging
  names are not published snapshot ids and are never
  retention candidates. `current` is never updated to a staging
  directory or a partial snapshot. Process death releases the kernel
  writer lease and may leave the staging directory and lock file; that
  leftover is not proof of ownership or deletion safety. Retention does
  not reap staging by guessed age. Cooperating readers take a shared
  lock on the same `.publish.lock` before resolving `current` and hold
  it until their snapshot files are materialized. Tools that ignore the
  lock are not protected. Strict readers, including MCP, reject a managed
  graph without `.publish.lock` and never create that file. `ByogGraph`, the
  doctor, and snapshot audit retain an explicit read-only compatibility path
  for immutable evidence published before the lock existed; that path does
  not claim a retention lease. A legacy flat-parquet directory also has no
  cooperating retention protocol. Adding the lock to a pre-lock managed
  graph is `graphrag-code adopt-publication-lock --offline-confirmed`: an
  explicit operator migration that creates only `.publish.lock` after a
  compatibility-mode doctor. Automatically touching the file would be
  unsafe because existing pre-lock readers cannot be discovered.
  `graphrag-code snapshot-activate --snapshot <id> --expected-current <id>
  --activate-confirmed` is the explicit mutating CLI that retargets only
  `current` onto an already-published retained snapshot. It takes a
  strict existing-lock exclusive lease on `.publish.lock` (never creating
  that file), validates the language-independent snapshot envelope, and
  atomically replaces the pointer. It is not deletion, retention,
  publication, repair, or reindex, and it is not an MCP tool.
  `graphrag-code snapshot-pins` / `snapshot-pin` / `snapshot-unpin` manage
  `<graph>/.snapshot-pins.json`. Operator pins are durable retention
  metadata for cooperating keep-last cleanup only. They are not backups,
  replication, activation, or a distributed lease. Listing never creates
  the file. Pin and unpin may create it only after confirmation, use
  compare-and-swap on the registry revision, and never create
  `.publish.lock`. Unpin does not delete immediately. A malformed
  registry fails closed before publication or cleanup mutates `current`
  or deletes snapshots.
  `graphrag-code snapshot-retention-plan --keep-last <N>` reports the
  shared keep-last decision without deleting anything. It is not prune,
  backup, replication, or an MCP tool.
  `graphrag-code snapshot-prune --keep-last <N> --expected-plan-revision
  sha256:<hex> --prune-confirmed` applies exactly that plan under an
  exclusive existing-lock lease. Recursive deletion is not
  transactionally atomic; a partial prune requires a fresh plan before
  retry. `graphrag-code snapshot-staging` lists `snapshots/.staging-*`
  entries as a read-only structural inventory and observes the private
  staging-writer lease without inferring ownership. Two-scan agreement
  is bounded change detection, not a liveness lease over a staging
  writer. Inventory `cleanup_eligible` stays false.
  `graphrag-code snapshot-staging-cleanup-plan` emits a schema-2
  read-only plan over that inventory. Schema 1 was read-only/pre-apply.
  Observed non-contention is not ownership and not the apply command's
  exclusive writer-lock claim. `graphrag-code snapshot-staging-cleanup
  --expected-plan-revision sha256:<hex> --cleanup-confirmed` applies
  exactly that CAS-verified plan. Recursive deletion is not
  transactionally atomic; a partial result requires a fresh plan.
  `graphrag-code snapshot-maintenance-plan --keep-last <N>` embeds
  both current plans under one shared existing-lock lease. It is not
  another mutation path. `graphrag-code snapshot-maintenance-apply
  --keep-last <N> --expected-maintenance-revision sha256:<hex>
  --maintenance-confirmed` applies that composite under one exclusive
  existing-lock lease: staging cleanup first, then prune. Applying
  either component, standalone or composite, requires a fresh plan
  before the next apply. `graphrag-code snapshot-maintenance-reconcile
  --plan-file <saved-plan.json>` observes the aftermath of a complete,
  partial, interrupted, or externally modified run. It does not
  recover or prove deletion cause. `graphrag-code snapshot-export-plan
  --snapshot <id|current>` reports the selected snapshot's direct
  envelope payload files with sizes and content hashes. It does not
  export, archive, or authorize deletion.
  `graphrag-code snapshot-export-apply --snapshot <id|current>
  --destination <new-dir> --expected-export-revision sha256:<hex>
  --export-confirmed` copies that CAS-verified payload set into a
  newly created destination. It does not mutate the graph or
  overwrite an existing path. Publication is bound to the held
  staging inode; a later parent-fsync or destination-identity
  failure emits `ok=false`, `partial=true`, and exit 1 without
  deleting the destination. A crash may leave the private
  sibling staging directory. Advisory locks protect only cooperating
  processes. None of these commands is an MCP tool.
- `scripts/persisted_graph_doctor.py` / `graphrag-code doctor` – **read-only
  persisted-integrity doctor** for any BYOG graph. Selects one snapshot,
  validates the language-independent envelope, then runs every applicable
  C overlay contract against that same loaded snapshot. `--indexer python`
  runs the envelope only; `--indexer c` runs the nine existing C component
  names; `--indexer auto` uses persisted `source_file` extensions and
  extractor provenance and fails closed on empty, mixed, or contradictory
  evidence. Does not invoke an extractor, compiler, or Clang; does not
  compare the graph with a fresh extraction; does not take the exclusive
  publication lock, remove `.staging-*` remnants, or rewrite graphs. On
  a managed snapshot graph with an existing lock it holds a shared
  `.publish.lock` reader lease across snapshot selection and the complete
  aggregate audit. Old immutable evidence without the lock is audited in
  explicit fingerprint-only compatibility mode and has no retention lease.
  A stable staging directory is reported as a publication notice, not as
  proven corruption. Exit 0 = every applicable persisted contract is
  valid, 1 = integrity violation or concurrent mutation, 2 = unsafe path /
  ambiguous auto-indexer / unreadable input. This is a persisted-state
  verifier, not a repair tool or a proof of semantic equivalence.
- `scripts/c_clang_type_audit.py` – type *declarations* (named complete
  structs, named complete enums, package-local typedefs) vs tree-sitter
  `struct` / `enum` / `typedef` entities. Collision-safe identity is
  kind + path + name + exact start line/column. The CLI remains diagnostic
  (no graph mutation). Publishing selected matched type-declaration *fields*
  into BYOG requires the separate explicit `--clang-types` flag (no
  `uses_type` edges). Anonymous / union / incomplete / outside-package /
  alternate-site residuals are counted explicitly. `--fail-on-mismatch` exits
  1 only for `tree_sitter_only` / `clang_only` / `ambiguous` /
  `macro_location_unsupported` (not for out-of-scope, anonymous, unsupported,
  outside-package, or alternate sites alone).
- `scripts/c_clang_type_graph_audit.py` – **read-only integrity audit** for
  already-persisted `clang_type_*` entity fields and the `clang_types`
  manifest block. Does not invoke Clang, read `compile_commands.json`, build
  an AST capture, reindex, publish, or rewrite graphs, and never repairs
  data. `--graph` (plus optional `--snapshot` / `--output`) resolves the
  snapshot, SHA-256-fingerprints every graph input before and after the run,
  and reports that read-only verification alongside the findings. `--output`
  is refused inside the audited graph root so report generation cannot
  invalidate that guarantee. The deterministic JSON exposes `state`,
  `classification`, `violations`, counts, provenance and limitations. Exit
  0 = valid (including `legacy_absent` and `off` with zero type fields),
  1 = integrity violations, 2 = unreadable graph/snapshot/manifest. A
  missing manifest block never legitimizes existing `clang_type_*` fields,
  a present `clang_types` key that is null/list/string is invalid (not
  legacy), `mode=off` with type fields is an error, and an enabled block
  with partial, corrupted, or extra fields is an error. Shared
  producer-contract helpers live in `c_clang_types.py`
  (`validate_persisted_type_overlay`). Published C graph health runs the
  same pure check without changing extractor comparison.
- `scripts/c_clang_type_use_audit.py` – type *uses* on declaration-bearing
  AST nodes (function returns, parameters, locals, fields, globals, typedef
  underlying types). The CLI is diagnostic; publishing aggregated `uses_type`
  edges requires the separate explicit `--clang-type-uses` flag. Owners/targets
  reuse the function and type-declaration audits. Locations are the
  declaration-bearing node (not proven exact type-token spans). C's tag
  namespace stays distinct: bare names resolve only as unique typedef spellings,
  while `struct T` / `enum T` use explicit tag spelling. `--fail-on-mismatch`
  exits 1 for `owner_unmatched` / `target_unresolved` / `ambiguous_target` /
  `macro_location_unsupported` only.
- `scripts/c_clang_type_use_graph_audit.py` – **read-only integrity audit** for
  already-persisted configured `uses_type` edges and the
  `clang_type_uses` manifest block. Does not invoke Clang, reindex, publish,
  or rewrite graphs. Exit 0 = valid (including legacy/off with zero configured
  edges), 1 = integrity anomalies, 2 = unreadable graph/snapshot/manifest.
  Shared producer-contract helpers live in `c_clang_type_uses.py`
  (`relationship_id`, `validate_persisted_type_use_overlay`). Published C
  graph health also runs this pure check without changing extractor comparison.
- `scripts/c_clang_type_shape_audit.py` – **diagnostic type-shape audit** for
  package-local named complete structs/enums already matched by the
  type-declaration audit. Compares **ordered direct member names** (fields /
  enumerators) between Clang and tree-sitter at the exact configured site.
  Raw qualType, enum values, and bit-field widths are evidence only – **not**
  size/alignment/offset/ABI/layout/FFI claims. The CLI itself performs no BYOG
  mutation and creates no graph edges; publishing selected `matched_shape`
  member evidence into BYOG requires the separate explicit
  `--clang-type-shapes` flag. Exit 1 only for internal shape mismatch buckets
  under `--fail-on-mismatch`.
- `scripts/c_clang_type_shape_graph_audit.py` – **read-only integrity audit**
  for already-persisted `clang_shape_*` entity fields and the
  `clang_type_shapes` manifest block. Does not invoke Clang, read
  `compile_commands.json`, build an AST capture, reindex, publish, or rewrite
  graphs, and never repairs data. `--graph` (plus optional `--snapshot` /
  `--output`) resolves the snapshot, SHA-256-fingerprints every graph input
  before and after the run, and reports that read-only verification alongside
  the findings. `--output` is refused inside the audited graph root so report
  generation cannot invalidate that guarantee. The deterministic JSON exposes
  `state`, `classification`, `violations`, counts, provenance and limitations.
  Exit 0 = valid (including `legacy_absent` and `off` with zero
  shape fields), 1 = integrity violations, 2 = unreadable
  graph/snapshot/manifest. A missing manifest block never legitimizes existing
  `clang_shape_*` fields, `mode=off` with shape fields is an error, and an
  enabled block with partial, corrupted, or extra fields is an error. Shared
  producer-contract helpers live in `c_clang_type_shapes.py`
  (`validate_persisted_type_shape_overlay`). Published C graph health runs the
  same pure check without changing extractor comparison.

AST audit CLIs remain available (each captures once internally). They are
**Clang only** (`cc` accepted only when `--version` proves Clang/Apple Clang).
None is points-to analysis, layout/ABI proof, multi-config coverage, or
production C/C++ completeness.

**C symbol identity (tree-sitter extractor):** within one collision-safe module
key, when two or more of `function` / `struct` / `enum` / `typedef` share a
bare C name, every colliding kind uses the qualified title
`module_key:entity_kind:name` (for example `cJSON:struct:cJSON` and
`cJSON:typedef:cJSON`). Non-colliding symbols keep the legacy
`module_key:name`. Typedef aliases nested in declarators (including
function-pointer typedefs such as `typedef int (*handler)(...)`) are extracted
by walking only declarator structure – not by scanning parameter lists. The
type audit / `--clang-types` overlay may match any exact tree-sitter
declaration site owned by a semantic graph entity (the graph still keeps one
canonical source-derived span; fields record both graph-canonical and
matched-site coordinates). Alternate unselected sites are observation-only
and are not claimed dead/inactive. This is single-config declaration evidence
– not a multi-config type graph and not a `uses_type` overlay.
