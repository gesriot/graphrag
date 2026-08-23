# Graph Schema & Provenance Model (MVP)

## Goals
- Every node and edge carries **provenance** so downstream agents and humans can trust or discount facts.
- Distinguish hard deterministic facts (from tree-sitter + clang / ast / cargo metadata) from LLM-inferred ones.
- The primary interchange format for GraphRAG is the **BYOG parquets** (`entities.parquet`, `relationships.parquet`, optional `text_units.parquet`).

## Core Tables (BYOG contract)
See official GraphRAG output schema + BYOG page for base columns.

We extend with code-specific columns (present on both entities and relationships where applicable):

- `source_file`: relative path in the original repo
- `span`: either "line:col-line:col", "def foo", or byte range
- `extractor`: "tree-sitter-python", "clang-ast", "manual", "llm-entity-v1", etc.
- `confidence`: float [0,1] – 1.0 for deterministic parser facts
- `is_deterministic`: bool – true when the fact can be re-derived from source without LLM

### Persisted snapshot envelope (read-only)

`scripts/byog_snapshot_graph_audit.py` validates the directory, core
`manifest.json` fields, and parquet census written by
`publish_byog_snapshot()` without invoking an extractor, compiler, or
overlay reconstruction. The check is language-independent and applies to
every BYOG indexer. Extra top-level manifest keys are allowed because
overlay blocks are stored there.

| Field | Producer contract |
| --- | --- |
| `schema_version` | strict integer `1` |
| `id` | nonempty safe snapshot id; equals `snapshots/<id>` when that layout is used |
| `created_at` | finite producer-style ISO datetime string; never compared with now |
| `counts` | exact keys `entities`, `relationships`, `text_units`, `call_observations`; strict non-negative integers equal to loaded row counts |
| `files` | exact ordered parquet list: the three required tables, then `call_observations.parquet` only when that table is nonempty |
| `total_size_bytes` | sum of those named parquet files only; excludes `manifest.json` and optional `settings.yaml` |
| `corpus_hash` | always `null`; the audit does not invent hashing semantics |
| `git_commit` | `null` or a canonical lowercase Git object id; never compared with the checkout |
| `source_root` | `null` or the persisted producer string; the path need not exist on the host |

`settings.yaml` is optional and is deliberately absent from `files` and
`total_size_bytes`. A flat graph without `snapshots/<id>` still validates
the manifest and tables; directory-identity comparison is reported
`unavailable` rather than fabricated. Undeclared parquet files, symlinked
snapshot entries, unexpected non-file entries, atomic `*.tmp` remnants,
and unsafe snapshot paths fail closed. Core file symlinks are rejected
before their targets are read. The graph-root entry point SHA-256-fingerprints every regular file
in the selected snapshot, `current`, and the snapshots-directory listing.
`--output` must be outside the audited graph root.
`published_graph_health.py` attaches this status as `snapshot_integrity`
for every non-frozen published graph and short-circuits a broken envelope
before fresh extraction or language-specific overlay checks.

### Persisted-integrity doctor (read-only)

`scripts/persisted_graph_doctor.py` and `graphrag-code doctor` select one
snapshot, fingerprint it, load it once, and run the shared aggregator
`validate_persisted_graph_integrity()`. The envelope is always first. An
invalid envelope short-circuits every C validator so a broken base
snapshot cannot masquerade as an overlay problem. `--indexer python`
attaches only `snapshot_integrity`. `--indexer c` then runs the nine
existing C components in documented order. `--indexer auto` accepts a
complete, unambiguous persisted signal from `source_file` extensions
(`.py` / `.pyi` / `.c` / `.h`) and extractor provenance (`tree-sitter-python` vs
`tree-sitter-c` / compiler / Clang extractors). C overlay manifest
blocks, including `mode=off`, are C evidence; their absence is not a
Python signal. Empty, mixed, unknown-extractor, or contradictory
evidence requires an explicit `--indexer`.

Top-level `n_anomalies` is the sum of the envelope total, each run
component total, and any top-level concurrency anomalies. Compatibility
aliases such as `violations` are not added again. A stable
`snapshots/.staging-*` entry is an informational publication notice. A
change to the snapshots listing, `current`, snapshot files, or
`.publish.lock` during the audit is a concurrent mutation. The doctor
takes a shared reader lease when that regular lock file exists and never
creates it. Old immutable evidence without the lock uses an explicit
fingerprint-only compatibility path with no retention guarantee. This is a
persisted-state verifier, not extractor freshness, repair, compiler re-audit,
or semantic equivalence.

### Snapshot publication transaction

`publish_byog_snapshot()` writes parquet, optional `settings.yaml`, and
`manifest.json` in a private `snapshots/.staging-<id>/` directory on the
same filesystem as `snapshots/`. Immediately after creating that
directory the publisher creates
`snapshots/.staging-<id>/.staging-writer.lock` and holds an exclusive
advisory writer lease for the complete staging-write interval. Staging
writes remain concurrent across publisher processes: each publisher has
its own staging directory and writer-lock file. In an already-managed
graph, reacquiring existing writer-lock metadata briefly holds the
graph-root lock shared and is nonblocking while gated; fresh publisher
lock creation is not gated. That gate ends before payload construction
and prevents a cleanup release/unlink race with a waiting cooperative
writer. The writer lease is still held while the publisher waits for the graph-root exclusive
`.publish.lock`. While that graph-root lock is held, the publisher
releases and removes the writer-lock metadata, then:

1. atomically renames the staging directory to `snapshots/<id>/`;
2. atomically replaces the `current` pointer;
3. keep-last-N retains **published** snapshot ids only.

The published snapshot and `manifest.files` never contain the
writer-lock file. Staging names start with `.staging-` and are not
published snapshot ids. Standalone `cleanup_old_snapshots()` uses the
graph-root lock and never deletes an active writer's staging directory.
A normal exception before publication releases the writer lease and
removes only that writer's staging directory. If the rename succeeds
but publishing `current` fails, the unpublished final directory is
removed and `current` stays on the previous snapshot.

Publication and retention refuse a symlinked `snapshots/` directory or
`.publish.lock` instead of following either outside the graph root. A
retention call against a missing graph remains a filesystem no-op.

Process death releases the kernel writer lease and may leave the
staging directory and lock file. That leftover is not proof of
ownership, writer death, or deletion safety. An observed staging
directory without `.staging-writer.lock` is legacy/unverifiable,
including the unavoidable directory-creation-to-lock window. Retention
does not reap staging directories by guessed age. This protocol does
not claim a distributed lease service, durable ownership, recovery, or
backup. Cooperating readers take a shared lock on the same
`.publish.lock` before resolving `current` and hold it until their
snapshot files are materialized, so keep-last retention waits.
`snapshot-activate` takes a strict exclusive lease on an
already-existing regular `.publish.lock` and never creates that file.
Tools that ignore the lock can still see a retired snapshot
disappear. The graph-root lock, the private staging-writer lease, and
the staging-name convention belong to the publication protocol, not to
`manifest.files` or `total_size_bytes`. An explicitly selected staging
path is not a valid published snapshot for the envelope audit. Strict
readers, including MCP, reject a managed graph without a regular
`.publish.lock`; general graph loading and integrity audits retain an
explicit compatibility path for immutable pre-lock evidence, with no
retention guarantee. A legacy flat-parquet directory has no cooperating
retention protocol.

### Adopting `.publish.lock` on a pre-lock managed graph

`graphrag-code adopt-publication-lock --graph <root> --indexer auto|python|c
--offline-confirmed` is the only product path that creates `.publish.lock`
on an existing managed graph. It is an explicit offline migration, never
an automatic MCP, doctor, or `ByogGraph` side effect.

`--offline-confirmed` is required to create the file. Passing it asserts
that no legacy reader that ignores `.publish.lock` is active, no legacy
publisher or retention process that ignores `.publish.lock` is active, and
future publishers will use the current lock-aware protocol. The program
cannot prove those conditions. Pre-lock processes never open the lock
file, so they cannot be discovered. Automatically touching
`.publish.lock` would split the locking domain: lock-aware publishers
would wait for lock-aware readers, while any still-running pre-lock
reader would keep reading without a lease, and a pre-lock publisher
could replace `current` or retain snapshots underneath that reader.

Without the flag, a managed graph missing the lock exits 2 and is not
modified. Before mutation the command runs the persisted-integrity
doctor in compatibility mode. Flat parquet directories, incomplete or
symlinked managed markers, unsafe lock pathnames, unreadable input, and
an unsupported lock backend fail closed. An invalid persisted graph
exits 1 and does not receive a lock.

The only pathname the command may create is `<graph>/.publish.lock`. It
uses exclusive creation (`O_CREAT | O_EXCL`) with `O_NOFOLLOW` /
`O_CLOEXEC` where available, validates the opened descriptor with
`fstat` and path identity with `lstat`, and never follows, replaces,
truncates, chmods, or rewrites an existing lock. If another lock-aware
process wins the create race, the command validates that regular file
and serializes through the exclusive protocol (`already_adopted`). A
newly created lock is never deleted after its pathname is exposed:
removing it while another process may be waiting on that inode would
split the locking domain. After adoption the doctor runs again under
the established exclusive hold without a nested shared lease. The result's
`payload_unchanged` value is an observed comparison of the doctor's pre/post
persisted-input fingerprints with the lock entry excluded. A cooperating or
legacy actor can make it false; the adoption implementation itself still
writes only the lock.

MCP remains strict: a managed graph without a regular `.publish.lock`
is still rejected at startup. Immutable published `byog_*` evidence
keeps the explicitly unleased compatibility path.

### Snapshot history and structural diff

`graphrag-code snapshot-history` lists published `snapshots/<id>/`
directories newest-first by canonical id. `.staging-*` entries are
bounded informational notices, never published history. Unexpected
symlinks, files, or other unsafe snapshot entries fail closed.
`--limit` defaults to 20 and has a hard maximum of 200. `total` is the
exact published-directory count even when the returned sample is
truncated. Each returned snapshot's persisted envelope is validated.
Staging notices retain the exact count and return at most 20 sorted
names with explicit `returned` and `truncated` fields.

`graphrag-code snapshot-diff --from <id|current> --to <id|current>`
compares `entities`, `relationships`, `text_units`, and optional
`call_observations` using the nonempty string `id` column. Added,
removed, and modified samples are truncated independently per table;
totals remain exact. Modified samples contain only the row id and the
sorted changed-field names. A missing field differs from explicit null,
and JSON booleans differ from numbers. Parquet null/NaN values become
JSON null; other non-finite values are rejected. Manifest comparison is a
deterministic added/removed/changed key summary, not a payload dump.
Comparing a snapshot with itself is an all-zero diff. This is
structural persisted-field comparison, not semantic equivalence.

Both commands hold one shared graph-root reader lease across resolving
`current` once, listing, loading, and computing the response. They
never create `.publish.lock`. Without the lock they exit 2 unless
`--allow-unlocked-legacy` is passed; that CLI-only option still
fingerprints inputs and reports that there is no retention guarantee.
MCP `snapshot_history` and `snapshot_diff` are scoped to the startup
graph and never enable the legacy path. A lock-ignoring actor can still
mutate files; pre/post fingerprints detect that and fail closed.

### Activating a retained published snapshot

`graphrag-code snapshot-activate --graph <root> --snapshot <published-id>
--expected-current <published-id> --activate-confirmed` changes only the
managed graph's `current` pointer. It is an explicit mutating CLI
operation, not deletion, retention, publication, repair, or reindex, and
it is intentionally absent from MCP. The fixed MCP tool set remains 11
read-only tools.

`--activate-confirmed` is required. Without it the command exits 2, prints
a controlled diagnostic to stderr, writes nothing to stdout, and changes
nothing. `--expected-current` is a compare-and-swap guard: while one
exclusive existing-lock lease is held, `current` is resolved exactly once
and the pointer is written only when that id still matches. A mismatch
exits 1 and does not write. Both `--snapshot` and `--expected-current`
must be explicit canonical published ids. `current`, paths, traversal,
separators, staging ids, empty strings, and aliases are rejected.
Activating the already-current snapshot is a successful idempotent no-op
with `changed=false`.

Activation is managed-layout only and requires an already-adopted regular
`.publish.lock`. A missing lock exits 2 and points at
`adopt-publication-lock`. The exclusive lease never creates, truncates,
rewrites, chmods, or replaces the lock. Symlinked, non-regular,
disappearing, or inode-swapped locks fail closed. The command holds that
one exclusive lease across resolving current, checking the expected id,
validating the target envelope, fingerprinting protected inputs,
atomically replacing `current`, and verifying the result. It does not
acquire a nested shared lease. The opened lock fd is checked against the
pathname again after the potentially blocking acquisition, so replacing
the lock while the process waits cannot silently split the locking domain.

The target is resolved strictly beneath `<graph>/snapshots/<published-id>`
and must pass the language-independent persisted snapshot envelope
(manifest identity, required parquet files, counts, files list, sizes,
optional `call_observations`, unexpected/symlinked entries). Staging
directories are bounded notices, never valid activation targets.
Unexpected unsafe `snapshots/` entries fail closed. An invalid target
exits 1 and leaves `current` unchanged.

The pointer update uses the same-directory temporary-file + atomic
replace mechanism. A failed write leaves the previous `current` usable
and cleans temporary files. No manifest, parquet, settings file, snapshot
directory, staging directory, or lock is altered. Previously current and
newer snapshots are not deleted. Backward and forward activation among
retained published ids are allowed; existing retention continues to
protect whichever snapshot is current.

Pre/post fingerprints cover published snapshot payloads, snapshot
membership, staging membership, and lock content/identity. The only
expected change is the exact `current` transition. A second protected-state
fingerprint and current-pointer check run immediately before the write. A
lock-ignoring actor that mutates protected inputs fails closed with exit 1;
there remains an unavoidable race against actors that deliberately ignore
the advisory protocol. Advisory locks protect only cooperating processes.

### Reading a retained snapshot without activating it

Query, context-pack, doctor, and status tools accept an optional selector:

- CLI: `--snapshot <published-id|current>` on `query-symbol`, `callers`,
  `callees`, `types-used-by`, `type-users`, `type-closure`, `neighbors`,
  `subgraph`, `dependency-order`, `impact`, `observations`, and
  `context-pack`.
- MCP: optional last argument `snapshot: str = "current"` on
  `graph_status`, `graph_doctor`, `query_symbol`, `callers`, `callees`,
  `neighbors`, `impact`, `type_closure`, and `context_pack`.

Historical reads do not require `snapshot-activate` and never change
`current`. Omitting CLI `--snapshot` preserves the existing default
current or legacy-flat read. `--snapshot current` explicitly selects the
snapshot named by `current`. An explicit published id is resolved
strictly beneath `<graph>/snapshots/<id>` without reading `current`.
Staging ids, traversal, separators, empty values, malformed ids, missing
snapshots, and symlinked snapshot or core paths fail closed. A legacy
flat-parquet directory may still be read when the selector is omitted,
but an explicit retained id fails because it has no retained history.

One shared graph-root `.publish.lock` lease is held across validation,
resolution, parquet load, and the complete response, so cooperating
keep-last retention cannot delete the selected snapshot during the
call. Explicit query/context CLI selectors require that existing regular
lock and never create it. Only their omitted-selector compatibility path
may read a pre-lock managed graph without a lease; that path has no
retention guarantee. MCP remains strict and remains exactly 11 read-only tools.
`snapshot_history` and `snapshot_diff` keep their own reference
contracts. This is not activation, publication, retention, repair,
reindex, natural-language search, or semantic equivalence.

### Operator-managed snapshot retention pins

`graphrag-code snapshot-pins --graph <root>` lists the operator pin
registry. `graphrag-code snapshot-pin <published-id> --graph <root>
--expected-registry-revision <token> --pin-confirmed` and
`snapshot-unpin` write only `<graph>/.snapshot-pins.json`. This is
retention metadata, not activation, publication, reindexing, backup,
replication, or a distributed lease. It is intentionally absent from
MCP. The fixed MCP tool set remains 11 read-only tools.

Canonical registry schema:

```json
{
  "schema_version": 1,
  "pins": [
    "<canonical-published-id>"
  ]
}
```

Pins are unique, UTF-8-byte-sorted published snapshot ids. The file is
capped at 64 KiB and 1000 entries. `current`, staging names, traversal,
separators, empty strings, unknown fields, and duplicate keys are
rejected. An absent file is an empty operator pin set with revision
`absent`. Listing and publishing never create the file. Pin and unpin
may create it only after confirmation. Unpinning the last entry writes
the canonical empty registry and does not unlink it. Symlinked or
non-regular registry paths fail closed. Malformed registry state is
never silently replaced.

The list result includes `schema_version`, `graph`, `current`,
`registry_revision`, `operator_pins`, `claim_pins`, and
`effective_pins`. `claim_pins` are existing doc-claim/frozen-evidence
pins. `effective_pins` is the sorted union. Registry revision is
`absent` or `sha256:<lowercase hex of the exact file bytes>`.

Pin and unpin require `--expected-registry-revision`. Under one exclusive
existing-lock lease the command recomputes that revision and refuses a
stale caller before writing. Pinning an already-pinned id or unpinning an
absent id succeeds with `changed=false`. Otherwise the canonical JSON is
written atomically and `changed=true`. Unpin reports that it performs no
immediate deletion and only makes the snapshot eligible for a later
cooperating keep-last operation.

Listing holds one shared existing-lock lease. Pin and unpin hold one
exclusive existing-lock lease across validation, CAS, atomic replacement,
verification, and the complete response. They never create, rewrite,
chmod, truncate, or replace `.publish.lock`. A missing lock points at
`adopt-publication-lock`. Advisory locks protect only cooperating
processes. Pin and unpin may change only `.snapshot-pins.json`.

Cooperating keep-last cleanup protects `current` UNION existing doc-claim
pins UNION operator pins. Publication validates the registry under the
publication lock before promoting a snapshot or replacing `current`. A
malformed registry aborts before those mutations. An absent registry
remains a no-op and is not created. Manual or lock-ignoring deletion can
still remove a pinned snapshot. The registry does not activate a
snapshot or claim semantic equivalence.

### Snapshot retention plan

`graphrag-code snapshot-retention-plan --graph <root> --keep-last <N>`
is a read-only report of what cooperating keep-last cleanup would
retain and delete. It shares `plan_snapshot_retention` with
`_cleanup_old_snapshots_locked`. The command is intentionally absent
from MCP. The fixed MCP tool set remains 11 read-only tools.

The effective protected set is `current` UNION existing doc-claim pins
UNION existing operator pins. `keep_last` has an effective minimum of
1. Current is always retained when it names a published directory.
Every existing claim or operator pin is retained even when that set
exceeds `keep_last`. Newest remaining published snapshots fill the
floor. Published order is UTF-8-byte ascending (chronological for
timestamped ids). Staging directories are never published ids and never
deletion candidates. Dangling pins (ids left by manual or lock-ignoring
deletion) are reported and are not invented as retained snapshots.

The planner loads and validates `.snapshot-pins.json` once under one
shared existing-lock lease. An absent registry is an empty operator pin
set and is not created. Malformed, oversized, symlinked, or
non-regular registry state exits 2 and changes nothing. A missing
`.publish.lock` exits 2 and points at `adopt-publication-lock`. The
command never creates, truncates, chmods, rewrites, or replaces the
lock. Advisory locks protect only cooperating processes.

JSON includes `schema_version`, `graph`, `keep_last_requested`,
`keep_last_effective`, `current`, `registry_revision`,
`published_count`, `published_snapshots`, `operator_pins`,
`claim_pins`, `effective_pins`, `existing_operator_pins`,
`existing_claim_pins`, `dangling_operator_pins`,
`dangling_claim_pins`, `retained_snapshots`, `deletion_candidates`,
`staging_notices`, and `plan_revision`. ID arrays are unique and
UTF-8-byte sorted; `published_snapshots`, `retained_snapshots`, and
`deletion_candidates` use that same canonical retention order.

`plan_revision` is `sha256:<lowercase hex>` of compact canonical JSON
(`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=True`, no
trailing newline) over the decision inputs:

```json
{
  "claim_pins": ["<id>"],
  "current": "<published-id>",
  "deletion_candidates": ["<id>"],
  "keep_last_effective": 1,
  "operator_pins": ["<id>"],
  "published_snapshots": ["<id>"],
  "registry_revision": "absent",
  "retained_snapshots": ["<id>"],
  "schema_version": 1
}
```

The schema version and exact retained/deletion decision are bound along
with their inputs so a later implementation cannot accept a token for a
different selection algorithm. Presentation fields and `plan_revision`
itself are excluded. `snapshot-prune` consumes that token as a
compare-and-swap guard. This command does not mutate `current`, snapshot
payloads, `.publish.lock`, or `.snapshot-pins.json`.

Before returning, the plan command rechecks `current`, the complete
published and staging listing, claim pins, the exact registry revision,
and the publication-lock identity. A detected lock-ignoring change exits
1. The registry is parsed only once; its second check safely hashes exact
bytes through an existing regular-file descriptor. Cooperating cleanup
rejects a missing or dangling `current` and any unexpected, symlinked, or
non-directory snapshot entry before deleting anything. Publication runs
the same cleanup-plan validation before promotion; if a non-cooperating
actor changes an input only after the new `current` is written, deletion
is skipped instead of acting on an ambiguous plan.

### Snapshot prune

`graphrag-code snapshot-prune --graph <root> --keep-last <N>
--expected-plan-revision sha256:<64 lowercase hex> --prune-confirmed`
applies exactly one recomputed retention plan. It shares
`plan_snapshot_retention` with planning and cleanup. There is no
dry-run; `snapshot-retention-plan` is the preview. Without
`--prune-confirmed` the command exits 2 and changes nothing. The token
must be `sha256:` plus 64 lowercase hex digits; whitespace, uppercase
hex, `absent`, and any other shape exit 2 before the lease.

The command requires a managed `current + snapshots/` graph and an
already-adopted regular `.publish.lock`. It never creates, truncates,
chmods, rewrites, or replaces the lock, and never creates
`.snapshot-pins.json`. One exclusive existing-lock lease covers
validation, plan recomputation, CAS, deletion, result construction,
serialization, stdout write, and stdout flush. It does not take a
nested shared lease and does not call `cleanup_old_snapshots`. A
missing lock exits 2 and points at `adopt-publication-lock`. Malformed,
oversized, symlinked, or non-regular registry state exits 2. A stale
`plan_revision` (including a keep-last mismatch or a changed
current/pin/published set) exits 1 and changes nothing. Advisory locks
protect only cooperating processes.

Deletion targets are exactly the CAS-verified `deletion_candidates`, in
canonical UTF-8-byte order. Current, retained snapshots, existing
operator pins, existing claim pins, staging directories, dangling pins,
paths outside `<graph>/snapshots/`, and any path reached through a
symlink are never deleted. Before the first deletion every candidate is
proven to be a canonical published id naming a real non-symlink
directory directly under `snapshots/`, the candidate set matches the
CAS-verified plan, and retained/current/pinned ids are disjoint from
candidates.

Recursive deletion of several directories is not transactionally
atomic. A later-candidate failure or process crash can leave a
partially applied prune. There is no rollback, trash, or recovery
protocol. Partial mutation writes a structured result then exits 1 with
`ok=false`, `partial=true`, `deleted_snapshots`, `failed_snapshot`, a
bounded `error`, `not_attempted_snapshots`,
`filesystem_may_have_changed=true`, and
`retry_requires_fresh_plan=true`. `changed` counts only candidates whose
complete directory removal succeeded. Because a recursive remover can
delete children before raising, every partial result conservatively says
the filesystem may have changed even when `changed=false`. A valid plan
with no candidates is an idempotent exit-0 result with `changed=false`
and `filesystem_may_have_changed=false`. Pre-deletion failures leave
stdout empty.

The command is intentionally absent from MCP. The fixed MCP tool set
remains 11 read-only tools.

### Snapshot staging inventory

`graphrag-code snapshot-staging --graph <root>` is a read-only
structural inventory of every direct `snapshots/.staging-*` entry. It
does not delete, rename, repair, quarantine, publish, activate, pin,
prune, or age-classify staging. It requires a managed
`current + snapshots/` graph and an already-adopted regular
`.publish.lock`. It never creates, truncates, chmods, rewrites, or
replaces that lock, and never creates or changes `.snapshot-pins.json`,
`current`, published payloads, or staging entries.

Publishers write `snapshots/.staging-*` outside the graph-root
publication lock and take that lock only for promotion. Cooperating
publishers hold a dedicated advisory writer lease on
`.staging-<id>/.staging-writer.lock` during that private write. The
shared graph lease therefore does not make staging contents immutable
and is not a liveness lease over a staging writer. The command inspects
the snapshots listing, staging metadata, and writer-lock identity/lease
state, then performs a second consistency scan before emitting a
result. Added, removed, replaced, type-changed, or
content-metadata-changed staging entries, and held/not-held or
writer-lock appearance/disappearance/replacement/type changes, exit 1
with empty stdout. That two-scan agreement is bounded change detection,
not a liveness lease over a staging writer. A stable listing is not
proof that a writer is dead. Ownership is always `unknown`. The command
does not use wall-clock age, mtimes, PID probing, process discovery,
host identity, or guessed timeouts to infer ownership. Cleanup is not
implemented; `cleanup_eligible` is always false. No backup, recovery,
quarantine, or distributed lease is claimed.

Entries are listed in canonical UTF-8-byte order. Each entry reports a
bounded structural summary: name, `candidate_snapshot_id` or null,
`name_valid`, `entry_kind`, top-level entry count and summaries,
presence of `manifest.json` / `entities.parquet` /
`relationships.parquet` / `text_units.parquet` and optional
`call_observations.parquet` / `settings.yaml`,
`complete_payload_candidate`, `writer_lease_protocol`
(`cooperative_v1` | `legacy_absent` | `unsafe`), `writer_lease_state`
(`held_by_cooperating_writer` | `not_held_at_scan` | `unverifiable`),
`writer_lock_present`, `writer_lock_regular`,
`ownership_status=unknown`, `cleanup_eligible=false`, and structural
notices. Schema version is 2. A contended nonblocking probe means only
that a cooperating process held the writer lease at that observation. A
successful acquire-and-release means only that the lease was not held
at that instant. Missing writer-lock metadata is legacy/unverifiable.
Symlinked or non-regular writer-lock metadata fails closed without
following the target. `complete_payload_candidate` means only that the
expected top-level file names are present. It does not claim parquet
validity, manifest integrity, successful publication, writer death, or
deletion safety.
Symlinked staging entries and symlinked top-level children fail closed
without following the target. Nested directories are reported without
recursion. Non-regular children are reported and are not opened.
Staging-entry count is capped at 64 and per-entry top-level count at
64. The reported published-snapshot list is capped at 4096, so every
array that contributes to the response has a hard bound. Counts are
enforced while descriptor-relative directory iterators are consumed,
not after an unbounded `list(...)`. Directory descriptors use
`O_NOFOLLOW` and are matched back to the pathname before and after the
scan. A platform without safe fd-relative scanning exits 2 instead of
falling back to a pathname traversal. Exceeding any bound exits 2.

`staging_revision` is `sha256:<64 lowercase hex>` over compact
canonical JSON binding `schema_version`, `current`,
`published_snapshots`, and the complete reported `staging_entries`.
Graph path and presentation-only fields are excluded. The token is
informational and reserved for a possible later protocol; this command
does not accept or apply it. A graph with no staging entries is a valid
exit-0 report with `staging_count=0`.

The command holds one shared existing-lock lease across discovery,
consistency checks, result construction, serialization, stdout write,
and stdout flush. It does not take a nested lease. Relative `--graph`
paths resolve from the invoking cwd. A symlinked graph root,
`snapshots/`, `current`, or publication lock is rejected without
following it. The command is intentionally absent from MCP. The fixed
MCP tool set remains 11 read-only tools.

### Snapshot staging cleanup plan

`graphrag-code snapshot-staging-cleanup-plan --graph <root>` is a
read-only schema-2 plan over the schema-2 staging inventory. Schema 1
was read-only/pre-apply (`apply_supported=false`) and is not accepted
by apply. Schema 2 sets `apply_supported=true` and keeps
`cleanup_applied=false`. The plan command does not delete, rename,
quarantine, repair, claim, or mutate staging entries. It reuses the
snapshot-staging two-scan scanner under one shared existing-lock lease
and does not take a nested graph-root lease. It requires an
already-adopted regular `.publish.lock` and never creates, truncates,
chmods, rewrites, or replaces that lock. It never creates or changes
`.snapshot-pins.json`, `current`, published snapshots, staging
directories, payload files, or writer-lock bytes/metadata.

A staging name appears in `deletion_candidates` only when the stable
observation is a real directory, the suffix is a canonical published
snapshot id, `writer_lease_protocol` is `cooperative_v1`,
`writer_lease_state` is `not_held_at_scan`, and the writer-lock file
is present and regular. Payload completeness is not a selection
condition. Every other direct `.staging-*` entry is a
`blocked_entries` row with one machine-readable reason:
`held_writer_lease`, `legacy_or_missing_writer_lock`,
`noncanonical_staging_name`, or `non_directory_staging_entry`.
Symlinked, non-regular, replaced, or disappearing writer-lock
metadata, and two-scan disagreement, still fail closed with exit 1
and empty stdout. They are not ordinary blockers.

`deletion_candidates` means only "candidate in this read-only plan".
It is not proof that a writer died, not ownership, not permission to
delete, not a durable lease, and not inventory `cleanup_eligible`.
Embedded `staging_entries` keep `cleanup_eligible=false`. A future
writer may acquire the private writer lease after the plan is
emitted. No age, mtime heuristic, PID, process discovery, host
identity, boot ID, or timeout is used.

`staging_state_revision` is `sha256:<64 lowercase hex>` over compact
canonical JSON of the internal two-scan consistency token: current
identity/content, publication-lock identity, published snapshot
listing, each staging entry's name/type/dev/inode/mode/mtime/size,
top-level child identities and metadata, and writer-lock identity,
type, presence, and observed lease state. The raw token is not
exposed. The hash detects an inode replacement that happens to leave
public inventory fields equivalent. `observed_staging_revision` is
the inventory `staging_revision`.

`plan_revision` is `sha256:<64 lowercase hex>` over compact canonical
JSON binding `schema_version`, `current`, `published_snapshots`,
`observed_staging_revision`, `staging_state_revision`,
`deletion_candidates`, `blocked_entries`, `ownership_inference`,
`cleanup_applied`, and `apply_supported`. Absolute graph path,
counts, notices, `ok`, and `staging_entries` are excluded. This
command does not accept or apply that token. `cleanup_applied` stays
false. `apply_supported` is true because
`graphrag-code snapshot-staging-cleanup --graph <root>
--expected-plan-revision sha256:<hex> --cleanup-confirmed` is the
separate CAS apply. That command acquires one exclusive existing-lock
graph lease, recomputes this schema-2 `plan_revision`, compares the
caller token, nonblockingly claims every selected existing writer
lock, revalidates the exact staged directory, writer-lock identity
and type, and bounded top-level structural token, and holds those
claims through deletion. The plan's `not_held_at_scan` observation is
not that exclusive claim. Schema-1 revisions are rejected. There is
no dry-run and no caller-supplied deletion list. Confirmation is
required even when the candidate set is empty. Recursive deletion is
not transactionally atomic. A partial result reports `partial=true`,
`filesystem_may_have_changed=true`, and
`retry_requires_fresh_plan=true`; there is no rollback, trash,
quarantine, or recovery. A fresh plan is mandatory after any partial
result. Apply-result schema version is 1. Both commands are
intentionally absent from MCP. The fixed MCP tool set remains 11
read-only tools.

### Snapshot maintenance plan

`graphrag-code snapshot-maintenance-plan --graph <root> --keep-last
<N>` is a read-only composite of the current
`snapshot-retention-plan` and the current schema-2
`snapshot-staging-cleanup-plan`. It is not another mutation path.
Standalone `snapshot-prune` and `snapshot-staging-cleanup` remain
available, and `snapshot-maintenance-apply` is the composite CAS
apply. The command is intentionally absent from MCP. The fixed MCP
tool set remains 11 read-only tools.

The command requires a managed `current + snapshots/` graph and an
already-adopted regular `.publish.lock`. It never creates, truncates,
chmods, rewrites, or replaces that lock. It holds exactly one shared
existing-lock lease from plan construction through JSON/plain
serialization, stdout write, and stdout flush. It does not take a
nested graph lease. Both embedded plans are computed inside that
same lease by the unlocked retention and staging-cleanup builders.
Standalone public output of those planners is unchanged.

JSON includes `schema_version`, `ok`, `graph`, `keep_last`,
`current`, `published_snapshots`, `retention_plan`,
`staging_cleanup_plan`, `maintenance_revision`,
`actionable_components`, `fresh_plan_required_after_any_apply`, and
`notices`. Schema version is 1. `retention_plan` is the exact public
retention-plan object for the same graph and `keep_last`.
`staging_cleanup_plan` is the exact public schema-2 cleanup-plan
object. Top-level `current` and `published_snapshots` must agree with
both embedded plans; disagreement is an integrity failure.

`actionable_components` is deterministic and contains only
`snapshot-prune` and `snapshot-staging-cleanup`. Each appears only
when its embedded plan currently has a non-empty deletion set. The
list uses fixed UTF-8-byte order. It does not recommend an apply
order. Applying either component can invalidate the other revision.
`fresh_plan_required_after_any_apply` is always true. The operator
must capture a fresh composite or standalone plan after every apply
before running another mutation.

`maintenance_revision` is `sha256:<lowercase hex>` of compact
canonical JSON (`sort_keys=True`, `separators=(",", ":")`,
`ensure_ascii=True`, no trailing newline) over:

```json
{
  "actionable_components": ["snapshot-prune"],
  "current": "<published-id>",
  "fresh_plan_required_after_any_apply": true,
  "keep_last": 1,
  "published_snapshots": ["<id>"],
  "retention_plan": {"plan_revision": "sha256:<hex>"},
  "schema_version": 1,
  "staging_cleanup_plan": {"plan_revision": "sha256:<hex>"}
}
```

Graph path, counts, notices, `ok`, and presentation-only embedded
fields are excluded. The plan command does not accept
`--expected-*-revision` or a confirmation flag. Use the composite
`maintenance_revision` with `snapshot-maintenance-apply`, or the
embedded component `plan_revision` tokens with standalone
`snapshot-prune` or `snapshot-staging-cleanup`.

`graphrag-code snapshot-maintenance-apply --graph <root> --keep-last
<N> --expected-maintenance-revision sha256:<hex>
--maintenance-confirmed` is the mutating counterpart. Apply-result
schema version is 1. The command requires a managed
`current + snapshots/` graph and an already-adopted regular
`.publish.lock`. It never creates, truncates, chmods, rewrites, or
replaces that lock. It holds exactly one exclusive existing-lock
lease from plan recomputation through CAS, preflight, mutation,
result construction, serialization, stdout write, and stdout flush.
It does not take a nested graph lease and does not call the public
or scope entry points of `snapshot-prune` or
`snapshot-staging-cleanup`. Confirmation is required even when both
deletion sets are empty.

Under that exclusive lease it recomputes the exact composite plan
with the unlocked builders and compares `maintenance_revision`
before any mutation. A mismatch is exit 1, empty stdout, and no
filesystem change. Before the first unlink it validates both
deletion sets, nonblockingly claims every selected existing writer
lock, revalidates every staging directory, structure token, and
writer-lock identity from the captured consistency tokens, and
revalidates retention inputs, `current`, the published listing,
pins, and publication-lock identity. It does not recompute the
ordinary cleanup plan after taking those claims. Internal execution
order is `snapshot-staging-cleanup` then `snapshot-prune`. That is
the apply command's conservative execution order, not a
recommendation on the read-only plan. Recursive deletion is not
transactionally atomic. A partial result reports
`partial=true`, `filesystem_may_have_changed=true`, and
`retry_requires_fresh_plan=true`, names completed and remaining
components, and is emitted on stdout with exit 1. There is no
rollback, trash, quarantine, backup, repair, or recovery. Complete
success, including an empty plan, returns `ok=true`. `changed` is
true only when at least one complete candidate deletion succeeded.
`fresh_plan_required_after_any_apply` is always true, including on
complete and empty success; `retry_requires_fresh_plan` additionally
identifies partial execution.
The result binds expected and observed `maintenance_revision` plus
the two observed embedded `plan_revision` tokens.

`graphrag-code snapshot-maintenance-reconcile --graph <root>
--plan-file <saved-plan.json> [--apply-result-file
<saved-result.json>]` is the read-only aftermath inspection.
Reconcile-result schema version is 1. `--plan-file` is required saved
schema-1 `snapshot-maintenance-plan` JSON. `--apply-result-file` is
optional saved schema-1 `snapshot-maintenance-apply` JSON. Both paths
are relative to the invoking cwd unless absolute. Only regular files
opened read-only without following symlinks are accepted, and each is
bounded at 1,048,576 bytes. Input loading, structural validation, and
plan/result cross-validation all finish before graph inspection. Plan
validation recomputes the composite and both embedded component
self-hashes, requires direct canonical published candidates and direct
staging candidates, and checks their relationship to current and
published history. Apply-result validation requires the exact schema-1
component order and candidate partition and cross-checks current,
published, planned, and remaining sets against the saved plan.
Malformed, oversized, symlinked, replaced, or structurally invalid
inputs fail with exit 2 and empty stdout. A structurally valid apply
result that refers to another plan is an integrity failure: exit 1 and
empty stdout.

The command requires a managed `current + snapshots/` graph and an
already-adopted regular `.publish.lock`. It never creates, truncates,
chmods, rewrites, or replaces that lock. It holds exactly one shared
existing-lock lease from graph inspection through result
construction, serialization, stdout write, and stdout flush. It does
not take a nested graph lease and does not call publishers,
extractors, repair/reindex, `snapshot-prune`,
`snapshot-staging-cleanup`, or `snapshot-maintenance-apply`.

Under that lease it performs a stable two-scan observation of
`current`, published snapshot names, publication-lock identity, and
every planned published and staging deletion-candidate pathname.
Direct entry kind and identity are read without following symlinks.
Lock-ignoring changes to `current`, the non-candidate published
listing, or the publication lock between scans are integrity
failures with exit 1 and empty stdout. Candidate pathnames that
change between scans are reported as `changed_during_reconcile`.
Writer-lock observation reuses the read-only staging inventory probe
and does not claim or create writer locks.

JSON includes `schema_version`, `ok`, `graph`, `input_plan_revision`,
`input_plan_valid`, `apply_result_supplied`, `apply_result_valid`,
`observed_current`, `observed_published_snapshots`,
`current_matches_saved_plan`, `reconciliation_is_observation_only`,
`deletion_cause_proven`, `recovery_performed`,
`published_candidate_observations`,
`staging_candidate_observations`,
`all_planned_candidates_absent_at_reconcile`,
`result_consistent_with_observation`, `discrepancies`, and
`notices`. `ok` means the read completed, not that maintenance
succeeded. Remaining candidates or discrepancies still exit 0.
Candidate states are `absent_at_reconcile`,
`present_directory_at_reconcile`,
`present_non_directory_at_reconcile`,
`unsafe_symlink_at_reconcile`, and `changed_during_reconcile`. An
absent pathname does not prove deletion cause:
`deletion_cause_proven` is always false and
`recovery_performed` is always false. When an apply result is
supplied, a declared-deleted candidate that is present, a failed or
not-attempted candidate that is not a present directory, and an
observed current different from the saved plan are discrepancies that
make `result_consistent_with_observation=false`.
Replacements cannot be proven identical from the saved public plan
or result. A new maintenance plan is still required before any later
mutation. The composite plan, apply, reconcile, export-plan,
export-apply, export-verify, export-reconcile, and export-staging
commands are
intentionally absent from MCP. The fixed MCP tool set
remains 11 read-only tools.

`graphrag-code snapshot-export-plan --graph <root> --snapshot
<id|current>` is a read-only inspection of one retained published
snapshot. Export-plan schema version is 1. `--snapshot` is required
and accepts exactly `current` or a canonical retained published
snapshot id. `current` is resolved once under the protected
interval. Managed `current + snapshots/` graphs with an
already-adopted regular `.publish.lock` only. The command never
creates, truncates, chmods, rewrites, or replaces that lock. Legacy
flat and unlocked compatibility are out of scope.

It holds exactly one shared existing-lock lease from snapshot
selection through validation, hashing, result construction,
serialization, stdout write, and stdout flush. It does not call a
public scope that takes a nested graph lease. Payload files are
opened relative to one anchored no-follow selected-directory
descriptor and hashed in two complete bounded-memory streaming passes.
A platform missing those descriptor-relative primitives is rejected.
The accepted payload
set is the published snapshot envelope: required
`manifest.json`, `entities.parquet`, `relationships.parquet`, and
`text_units.parquet`; `call_observations.parquet` only when that
file is present; optional `settings.yaml` when present. Unexpected
or non-regular snapshot entries fail closed. Candidate names and
manifest-declared filenames must be canonical direct names. Symlinks
are never followed.

JSON includes `schema_version`, `ok`, `graph`,
`requested_snapshot`, `resolved_snapshot`, `snapshot_path`,
`files`, `file_count`, `total_size_bytes`, `export_revision`,
`export_performed=false`, `fresh_plan_required_before_export=true`,
and `notices`. `files` is UTF-8-byte relative-path order. Each
record has `path`, exact `size_bytes`, and
`sha256:<64 lowercase hex>` `content_revision`.
`export_revision` is `sha256:<hex>` of compact canonical JSON
(`sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=True`, no
trailing newline) over:

```json
{
  "files": [
    {
      "content_revision": "sha256:<hex>",
      "path": "entities.parquet",
      "size_bytes": 0
    }
  ],
  "resolved_snapshot": "<published-id>",
  "schema_version": 1
}
```

Graph and snapshot absolute paths, `requested_snapshot`,
`file_count`, `total_size_bytes`, `ok`, notices, and the boolean
flags are presentation-only. `export_revision` is a
self-consistency token for this exact observed payload. It does not
prove provenance, authenticity, recoverability, or that a future
export apply may proceed without a fresh plan. The plan is not a
backup and is not authorization to delete anything.
`export_performed` is always false. Differences visible across the two
payload hash observations and the publication-lock, requested-current,
snapshot-listing, selected-directory, and manifest rechecks are
integrity failures: exit 1, empty stdout. Advisory locking is not
continuous protection against lock-ignoring changes after the final
observation.
Ordinary invalid selectors or unsupported layout are exit 2, empty
stdout. The command is intentionally absent from MCP. The fixed MCP
tool set remains 11 read-only tools.

`graphrag-code snapshot-export-apply --graph <root> --snapshot
<id|current> --destination <new-dir> --expected-export-revision
sha256:<64 lowercase hex> --export-confirmed` is the CAS-guarded
copy of that same accepted payload set into a newly created
standalone destination directory. Export-apply schema version is 1.
`--snapshot`, `--destination`, `--expected-export-revision`, and
`--export-confirmed` are required. The expected revision must be
exactly `sha256:<64 lowercase hex>` with no whitespace
normalization. A saved plan file is not accepted. `current` is
resolved once under the protected interval. Managed
`current + snapshots/` graphs with an already-adopted regular
`.publish.lock` only. The command never creates, truncates, chmods,
rewrites, or replaces that lock, and it never mutates `current`,
published snapshots, pins, staging, manifests, or payloads. Legacy
flat and unlocked compatibility are out of scope.

The destination parent must already exist and be a real directory.
The final destination pathname must be absent; existing regular
files, directories, symlinks, and other entry types fail closed
without modification. A relative destination is resolved from the
invoking cwd and reported as its canonical anchored location.

It holds exactly one shared existing-lock lease from snapshot
selection through fresh export-plan recomputation, expected-revision
comparison, source reopening, copying, destination verification,
atomic publication, result construction, serialization, stdout
write, and stdout flush. It does not call a public scope that takes
a nested graph lease. The selected snapshot stays anchored by a
no-follow directory descriptor. Payload files are opened
descriptor-relative and streamed in bounded memory. The copy
independently verifies each source file's byte count and SHA-256
against the fresh plan. The complete source payload is reobserved
after copying and before publication. Directory replacement must
not redirect reads outside the selected snapshot. Apply uses the
fresh plan's final lock/current/listing/selected-directory token
set; it does not establish a later unbound baseline. A stable lock
replacement, current retarget, or snapshots-listing change after
that plan observation is an integrity failure. When
`requested_snapshot` is `current`, the observed current value must
remain the resolved snapshot id.

Publication uses a private unpredictable sibling staging directory
created descriptor-relative under an anchored destination-parent
FD. Immediately after that directory is anchored, apply creates
the empty regular protocol file `.export-writer.lock` with
restrictive exclusive `O_NOFOLLOW` creation and holds one blocking
exclusive advisory writer lease for payload creation, source
copying and hashing, payload fsync, source reobservation, staged
envelope verification, and the wait immediately before
publication. This is not a public scope and not a graph lease; the
managed source graph continues to use exactly one shared existing
publication-lock lease. Before atomic publication the held staging
directory, writer-lock pathname, writer-lock descriptor, and
captured identities are revalidated, the exact owned lock metadata
is removed while its lease is still held, the lease is then released,
the staging directory is fsynced, the
lock pathname is proven absent, and the staging directory is
reverified to contain exactly the export envelope. The published
destination never contains `.export-writer.lock`. The advisory
lease is not externally observable after that pathname is removed.
The lock file is protocol metadata, not proof of ownership, writer
identity, writer death, crash, or cleanup eligibility. Restrictive
creation modes, exclusive payload-file creation, fsync of
completed files and the staging directory, descriptor-relative
staged verification, and an atomic no-replace directory rename
then proceed as before. Immediately before that rename the staging
pathname's no-follow identity must still be the held staging
descriptor and the originally captured staging inode. After rename
returns, the destination pathname is inspected descriptor-relative
and must be that same inode; a final descriptor-relative file-set,
size, and SHA-256 verification uses the held/published directory
descriptor. `destination_verified=true` only after those checks
pass. A check-then-rename that can replace a
concurrently created empty directory is not acceptable. A platform
missing those descriptor-relative or exclusive-publication
primitives is rejected. Before-publication failures leave the final
destination absent. Best-effort cleanup may remove only the exact
private staging directory, this invocation's writer-lock metadata
when staging-directory, lock-path, and captured creation identities
still match, and direct children created by this invocation. It
never recursively deletes an unresolved or replaced pathname, and
it never rmdirs a staging pathname whose identity is unknown or
mismatched. A crash before lock creation may leave a staging
directory with missing metadata. A crash after lock creation may
leave the regular lock file; process death releases the kernel
lease but does not remove the file. This command does not add a
staging cleanup tool. After atomic publication succeeds, a later
reporting or parent-fsync failure never deletes the destination.

JSON includes `schema_version`, `ok`, `graph`,
`requested_snapshot`, `resolved_snapshot`, `destination`, `files`,
`file_count`, `total_size_bytes`, `expected_export_revision`,
`observed_export_revision`, `export_confirmed`,
`export_performed`, `destination_created`, `destination_verified`,
`source_unchanged`, `partial`, `parent_fsync_confirmed`, `error`,
and `notices`. `files` preserves the
export-plan UTF-8-byte order. Each record has `path`,
`size_bytes`, and `sha256:<64 lowercase hex>`
`content_revision`. On complete success `ok=true`,
`partial=false`,
`export_performed=true`, `destination_created=true`,
`destination_verified=true`, `source_unchanged=true`,
`parent_fsync_confirmed=true`, and
`expected_export_revision == observed_export_revision`. A failure
after successful atomic publication emits `ok=false`,
`partial=true`, `export_performed=true`,
`destination_created=true`, the actual verification flags, a
bounded `error`, and exit 1; the destination is never deleted.
The copy
is not a backup, authentic, recoverable, or authorization to
delete anything. It does not preserve ownership, timestamps,
xattrs, ACLs, hardlinks, or provenance.
Ordinary usage, confirmation, layout, destination,
unsupported-platform, or CAS refusals are exit 2, empty stdout.
Integrity or concurrency failures before publication are exit 1,
empty stdout.
A fully emitted successful result exits 0. The command is
intentionally absent from MCP. The fixed MCP tool set remains
11 read-only tools.

`graphrag-code snapshot-export-verify --export-dir <directory>
--expected-export-revision sha256:<64 lowercase hex>` is the
read-only check that one already-created standalone export
directory still contains exactly the snapshot envelope bound by
that revision. Export-verify schema version is 1.
`--export-dir` and `--expected-export-revision` are required. The
expected revision must be exactly `sha256:<64 lowercase hex>`
with no whitespace normalization. Relative `--export-dir` is
resolved from the invoking cwd and reported as its canonical
anchored path. The final export path must be an existing real
directory, not a symlink. The command does not inspect a managed
graph, read `current`, `snapshots/`, pins, staging, or
`.publish.lock`, or acquire a graph lease. It does not mutate the
export directory or any graph.

It anchors the export directory with a no-follow directory
descriptor. All listing, stat, open, and read operations are
descriptor-relative. A platform missing those primitives is
rejected. The accepted payload set is the published snapshot
envelope: required `manifest.json`, `entities.parquet`,
`relationships.parquet`, and `text_units.parquet`;
`call_observations.parquet` only when present; optional
`settings.yaml` when present. Unexpected entries, symlinks,
non-regular payloads, and noncanonical or nested names fail
closed. `resolved_snapshot` is derived from `manifest.id`. File
records use the snapshot-export-plan fields `path`, `size_bytes`,
and `content_revision`. `observed_export_revision` is computed
through the existing canonical export-revision helpers and is
byte-for-byte compatible with snapshot-export-plan and
snapshot-export-apply. Payloads are hashed in two complete
bounded-memory streaming observations of directory identity,
exact direct listing, manifest identity/content, and every
payload identity, size, and SHA-256. The second payload pass is
bracketed by directory/listing observations. Same-size content
replacement with restored mtime is detected. Directory
replacement must not redirect reads outside the original
anchored directory. The descriptor is held through result
construction, serialization, stdout write, and stdout flush.

JSON includes `schema_version`, `ok`, `export_directory`,
`resolved_snapshot`, `files`, `file_count`, `total_size_bytes`,
`expected_export_revision`, `observed_export_revision`,
`revision_matches`, `payload_verified`, `export_mutated=false`,
`graph_inspected=false`, and `notices`. When the observed
revision matches, `ok=true`, `revision_matches=true`,
`payload_verified=true`, and the command exits 0. When the
directory is structurally valid and stable but the expected
revision does not match, the complete report is emitted with
`ok=false`, `revision_matches=false`, `payload_verified=true`,
and exit 1. Malformed arguments or unsupported
invocation/platform conditions are exit 2, empty stdout. Unsafe
structure, symlinks, invalid envelope content, or concurrent
changes are exit 1, empty stdout. The verification is not a
backup, authentic, recoverable, complete source evidence, or
authorization to delete anything. The command is intentionally
absent from MCP. The fixed MCP tool set remains 11 read-only
tools.

`graphrag-code snapshot-export-reconcile --plan-file
<saved-plan.json> --destination <path>
[--apply-result-file <saved-apply-result.json>]` is the read-only
aftermath inspection for a saved schema-1 snapshot-export-plan and
an optional saved schema-1 snapshot-export-apply result. Export-reconcile
schema version is 1. `--plan-file` and `--destination` are
required. Relative paths resolve from the invoking cwd. Saved JSON
inputs must be bounded regular files (1 MiB), opened read-only
without following symlinks. Input loading and complete plan/result
validation finish before the destination is inspected. The command
does not inspect a managed graph, read `current`, `snapshots/`,
pins, staging, or `.publish.lock`, or acquire a graph lease. It
does not recover, retry, copy, repair, rename, quarantine, delete,
import, restore, or mutate anything.

Plan validation covers the complete producer contract: schema
version, booleans, canonical snapshot id, canonical direct file
names, unique UTF-8-byte-ordered files, exact `file_count` and
`total_size_bytes`, exact `sha256:<64 lowercase hex>` content
revisions, the required/optional snapshot-envelope file set,
`export_performed=false`, and an exact recomputed `export_revision`
from the snapshot-export-plan canonical helper. Presentation fields
and notices never replace that validation. A structurally valid
apply result must match the saved plan's revision, resolved
snapshot, ordered files, counts, and sizes, and its destination
must equal the canonical anchored `--destination`. Its
confirmation/export/destination/source/parent-fsync/error flags
must form either an exact complete-success outcome or an exact
emitted post-publication partial outcome.

The destination parent is anchored with a no-follow directory
descriptor. Destination listing, stat, open, and read operations
are descriptor-relative. A platform missing those primitives is
rejected. Destination observation reuses the snapshot-export
verification hashing/listing contract without invoking a public CLI
or creating a nested observation window. Symlinks, non-direct
structure violations, invalid envelopes, and concurrent
replacement/change fail closed and never redirect reads outside the
anchored destination. The relevant parent/export descriptor stays
held through result construction, serialization, stdout write, and
stdout flush.

JSON includes `schema_version`, `ok`, `input_plan_revision`,
`input_plan_valid`, `apply_result_supplied`, `apply_result_valid`,
`declared_apply_outcome` (`not_supplied`, `complete`, or
`partial`), `destination`, `destination_state` (`absent`,
`matches_plan`, or `revision_mismatch`), `destination_present`,
`destination_matches_plan`, `resolved_snapshot`, `files`,
`file_count`, `total_size_bytes`, `observed_export_revision`,
`export_mutated=false`, `graph_inspected=false`,
`recovery_performed=false`, `creation_cause_proven=false`, and
`notices`. `ok=true` means the observation completed, not that
apply succeeded or that the destination matches. Stable absence and
stable revision mismatch therefore emit a complete report and exit
0. Unsafe structure, invalid envelope content, input/result
cross-integrity failure, or concurrent destination change is exit
1, empty stdout. Ordinary argument, input-format, or
unsupported-platform errors are exit 2, empty stdout.

Absence does not prove snapshot-export-apply failed or that another
actor deleted the destination. Presence does not prove
snapshot-export-apply created it. Revision equality proves only
equality with the saved plan's canonical payload contract during
the observation window. A fresh export plan is still required
before any later apply. Reconciliation performs no recovery and
authorizes no deletion. The command is intentionally absent from
MCP. The fixed MCP tool set remains 11 read-only tools.

`graphrag-code snapshot-export-staging --parent <directory>` is the
read-only structural inventory of private snapshot-export-apply
staging names under one selected parent. Export-staging schema
version is 1. `--parent` is required and is resolved from the
invoking cwd. The parent must be an existing real directory, not a
symlink. The command anchors that parent with a no-follow
directory descriptor before canonicalization and confirms the
canonical path still names the held descriptor. All listing and
child stat operations are descriptor-relative with
`follow_symlinks=False`. The parent descriptor stays held through
result construction, serialization, stdout write, and stdout flush.
The complete parent identity token includes metadata capable of
exposing rename-away-and-back activity and is not reduced to only
`dev/ino`. A platform missing those primitives is rejected.

Two complete bounded scans of the direct parent listing must
agree. The command does not recurse and does not open or read
export payload contents. It does not inspect a managed graph, read
`current`, `snapshots/`, pins, or `.publish.lock`, or acquire a
graph lease. For recognized real directories only it may open the
staging directory descriptor-relative and inspect or
nonblocking-probe the fixed `.export-writer.lock` protocol entry.
It never creates, writes, truncates, chmods, replaces, unlinks, or
renames lock metadata, and it never follows a lock-file symlink.
Unrecognized prefixed entries and recognized non-directory entries
are not probed. Current-protocol names match
`^\.graphrag-export-[0-9a-f]{32}$` exactly. Direct entries that
start with `.graphrag-export-` but do not match that syntax are
reported as unrecognized prefixed entries and are never silently
treated as staging created by this project. Unrelated child names
are counted only.

JSON includes `schema_version`, `ok`, `parent`, `staging_entries`,
`staging_count`, `unsafe_staging_count`,
`unrecognized_prefixed_entries`, `unrecognized_prefixed_count`,
`other_entry_count`, `inventory_revision`, `ownership_known=false`,
`writer_activity_known=false`, `cleanup_supported=false`,
`cleanup_performed=false`, `parent_mutated=false`,
`graph_inspected=false`, and `notices`. Each recognized or
unrecognized prefixed record includes `name`, `path`, `kind`,
`name_matches_current_protocol`, `ownership=unknown`,
`writer_activity=unknown`, `cleanup_eligible=false`,
`contents_inspected=false`, and no-follow identity/metadata
(`dev`, `ino`, `mode`, `size`, `mtime_ns`, `ctime_ns`). Each
recognized real directory also reports `writer_lease_state`
(`metadata_absent`, `metadata_unsafe`, `held_at_scan`, or
`not_held_at_scan`), `writer_lease_metadata_present`,
`writer_lease_contended`, `writer_lease_path`, `writer_lease_dev`,
`writer_lease_ino`, `writer_lease_mode`, `writer_lease_size`,
`writer_lease_mtime_ns`, and `writer_lease_ctime_ns`. For
`metadata_absent`, those identity fields are JSON null;
`writer_lease_path` is still the expected protocol pathname.
`held_at_scan` does not change `writer_activity`. Probe state and
lock identity participate in both scans and `inventory_revision`.
`inventory_revision` is a self-consistency hash of the canonical
observed recognized/unrecognized records and counts. It is not a
cleanup CAS token, ownership proof, writer lease, or authorization
to mutate anything.

`ok=true` means the inventory observation completed. Stable
inventories, including stable symlink or non-directory prefixed
entries, emit the complete report and exit 0. Those unsafe kinds
are observations, not paths to follow. Malformed arguments, a
missing/non-directory/symlinked parent, exceeded bounds, or an
unsupported platform are exit 2, empty stdout. Concurrent listing,
entry-identity, entry-metadata, parent-identity, or pathname
changes are exit 1, empty stdout.

A matching name does not prove snapshot-export-apply created the
entry. A stable observation does not prove a writer is absent or
dead. Absence does not prove cleanup occurred. Inventory performs
no cleanup and does not recommend deletion. Contents are not
inspected, so this is not export verification. This is not a
backup, recovery, authenticity, provenance, or recoverability
claim. Changes after the final observation are outside the
observation window. The command is intentionally absent from MCP.
The fixed MCP tool set remains 11 read-only tools.

`graphrag-code snapshot-export-staging-cleanup-plan --parent
<directory>` is the read-only schema-2 cleanup plan over that same
inventory. Surfaces are also
`python -m graphrag_code.snapshot_export_staging_cleanup_plan` and
`scripts/snapshot_export_staging_cleanup_plan.py`. It reuses the
export-staging descriptor-relative, no-follow, bounded two-scan
observation scope. It does not invoke a public CLI, does not
perform a second path-based scan, and does not inspect a managed
graph. The parent descriptor plus retained recognized staging and
lock descriptors stay open through plan construction,
serialization, stdout write, and flush. Continuous protection is
not claimed after that observation scope ends. Inventory schema 1
is unchanged: `cleanup_supported` stays false, recognized-directory
`cleanup_eligible` stays false, `contents_inspected` stays false,
and `writer_activity` stays unknown. `held_at_scan` is only
instantaneous cooperative contention. `not_held_at_scan` is not
ownership, writer death, abandonment, age, permission to delete,
or the apply command's exclusive writer-lock claim. Cleanup-plan
schema 1 was read-only/pre-apply (`apply_supported=false`) and is
not accepted by apply. Schema 2 sets `apply_supported=true` and
keeps `cleanup_applied=false`.

JSON includes `schema_version`, `ok`, `parent`,
`observed_inventory_revision`, `staging_entries`, `staging_count`,
`unrecognized_prefixed_entries`, `unrecognized_prefixed_count`,
`other_entry_count`, `deletion_candidates`,
`deletion_candidate_count`, `blocked_entries`, `blocked_count`,
`ownership_inference=false`, `cleanup_applied=false`,
`apply_supported=true`, `plan_revision`, and `notices`. A
deletion candidate is reported only when the direct child name
matches `^\.graphrag-export-[0-9a-f]{32}$`, the held descriptor
observes a real directory, `writer_lease_state` is
`not_held_at_scan`, lock metadata is present, non-contended, a
safe regular single-linked empty file, and the lock mode has no
group or other permission bits, and both scans plus retained
identities agree. Export payload contents are not opened to
classify a candidate. Every other export-staging-prefixed entry is
blocked, not omitted, with one of
`non_directory_staging_entry`, `unrecognized_staging_name`,
`writer_lease_metadata_absent`, `writer_lease_metadata_unsafe`,
`held_writer_lease`, `nonempty_writer_lease_metadata`,
`permissive_writer_lease_metadata`, or
`unverifiable_writer_lease_state`. Unrelated parent entries remain
counted only. Names are ordered by raw filesystem bytes.

`deletion_candidates` means only selected by this read-only plan.
It is not ownership, cleanup eligibility, proof that apply created
the directory, proof that a writer died, or authorization to
delete. A writer may start after the plan is emitted.

`plan_revision` is `sha256:` plus the SHA-256 of compact UTF-8
JSON (`sort_keys=True`, `separators=(",", ":")`,
`ensure_ascii=True`, `allow_nan=False`, no trailing newline) of
exactly these decision keys: `schema_version`,
`observed_inventory_revision`, `deletion_candidates`,
`blocked_entries`, `ownership_inference`, `cleanup_applied`, and
`apply_supported`. Presentation fields such as `ok`, `parent`,
`notices`, redundant counts, and `staging_entries` are excluded.
`observed_inventory_revision` already binds the complete inventory
observation, including writer-lease state and lock identity.
Because `schema_version` and `apply_supported` participate in that
payload, schema-1 revisions differ from schema-2 revisions. This
command does not accept `--expected-plan-revision`, a confirmation
flag, a saved plan file, or an apply result. `cleanup_applied`
stays false. `apply_supported` is true because
`graphrag-code snapshot-export-staging-cleanup --parent
<directory> --expected-plan-revision sha256:<hex>
--cleanup-confirmed` is the separate CAS apply. Surfaces are also
`python -m graphrag_code.snapshot_export_staging_cleanup` and
`scripts/snapshot_export_staging_cleanup.py`. That command
anchors the selected parent as a real no-follow directory, holds
that descriptor through recomputation, comparison, claim,
revalidation, deletion, result serialization, stdout write, and
stdout flush, recomputes the complete schema-2 plan from a fresh
bounded two-scan observation, compares `plan_revision`,
nonblockingly claims every selected existing `.export-writer.lock`
before the first deletion, revalidates parent, staging-directory,
and lock identities, and deletes only those candidates.
Descriptor-relative no-follow recursion unlinks symlinks as
symlinks and never follows them. Schema-1 revisions are rejected.
There is no dry-run and no caller-supplied deletion list.
Confirmation is required even when the candidate set is empty.
There is no graph lease because this operates on an arbitrary
export parent. Recursive deletion is not transactionally atomic.
A partial result reports `ok=false`, `partial=true`,
`filesystem_may_have_changed=true`, and
`retry_requires_fresh_plan=true`; there is no rollback, trash,
quarantine, or recovery. A fresh schema-2 plan is mandatory after
any partial result. Apply-result schema version is 1. Export-staging
reconcile is not part of this milestone.

Stable candidates and stable blocked entries emit the complete
plan report and exit 0. Unsafe parent structure, descriptor/path
identity change, or concurrent observation change is exit 1,
empty stdout. Malformed arguments or unsupported safe primitives
are exit 2, empty stdout. A complete empty or nonempty apply
exits 0. A failure before the first mutation emits no stdout
(exit 1 for stale revision, contention, or identity/integrity;
exit 2 for malformed arguments, missing confirmation, unsafe or
missing parent, bounds, or unsupported primitives). Once any
unlink/rmdir may have happened, apply emits a complete partial
result and exits 1. Both commands are intentionally absent
from MCP. The fixed MCP tool set remains 11 read-only tools.

The plan command does not delete, rename, quarantine, pin,
activate, publish, repair, reindex, or create a lock. It does not
change `current`, published snapshots, `.snapshot-pins.json`,
staging directories, payload files, writer-lock bytes/metadata, or
publication-lock metadata. Apply deletes only the recomputed
deletion candidates under the selected parent. Neither command
inspects a managed graph, export destination, or export
authenticity. Advisory locks protect only cooperating processes.
Non-cooperating processes remain outside the protection boundary.
No ownership, liveness, backup, authenticity, or recovery is
claimed. Fresh publisher writer-lock creation remains concurrent.
Reacquiring existing writer-lock metadata stays nonblocking while
gated.

## Entity Types (code domain, start with these)
- file
- module / package
- function / method
- class / struct / enum / trait
- type_alias
- constant / variable (top level)
- test (special for golden traces)

### C module keys (conditional, collision-safe)

C file and symbol titles use a **module key** prefix shared by `scripts/extract_c.py`
and `scripts/c_compiler_facts.py` (`scripts/c_identities.py`):

1. Index every package `.c` / `.h` file.
2. If `path.stem` appears under only one parent directory, the module key is that
   stem (legacy-compatible: `cJSON.c` + `cJSON.h` → `cJSON`).
3. If the same stem appears under multiple parents, each parent group uses the
   package-relative POSIX path of `parent/stem` (no suffix), e.g.
   `src/left/util.c` → `src/left/util`, `src/right/util.c` → `src/right/util`.

Titles remain `{module_key}:{filename_or_symbol}`. Entity IDs embed the full
title (not a lossy slug alone). Packages without stem collisions keep the same
graph identities as before. Indexed paths that resolve outside the package, or
multiple package paths that resolve to the same file, fail explicitly rather
than receiving a guessed identity.

## Relationship Types (or rich description + type column)
- contains (file→function, module→symbol)
- calls / is_called_by
- imports / depends_on
- implements / inherits
- uses_type
- defines
- tests (test entity → symbol under test)

### Optional C compiler overlays (each default **off**)

Two independently selectable compiler layers share compile-DB reconstruction
helpers (`scripts/c_compiler_common.py`) and collision-safe file titles
(`scripts/c_identities.py`). Neither is clang AST type resolution. Both are
GNU/Clang-specific adapters (`-M` / `-E -H`), not a universal compiler API.

#### Flattened TU dependency (`depends_on`)

When `scripts/index_c.py --compiler-dependencies` is enabled (default **off**),
the tree-sitter-c graph gains flattened file→file `depends_on` edges from each
`compile_commands.json` translation unit to package-local `.c`/`.h` paths the
configured compiler reports under dependency-generation mode (`-M`, followed
by strict package-path filtering so package-local `-isystem` headers remain
visible while system/outside-package paths do not).

| Field | Value |
| --- | --- |
| `type` | `depends_on` |
| `source` / `target` | File-entity titles (`{module_key}:{filename}`) |
| `fact_kind` | `translation_unit_dependency` |
| `extractor` | `c-compiler-deps` |
| `confidence` / `is_deterministic` | `1.0` / `true` **only** relative to the recorded toolchain + compile DB |
| Description | Compiler/configuration-derived; may be **transitive** |

**Persisted integrity audit (read-only):**
`scripts/c_compiler_dependency_graph_audit.py` validates already-published
flattened `depends_on` relationships against the producer contract without
invoking a compiler, reading `compile_commands.json`, reading C/header
sources, running `compiler -M`, reconstructing dependency sets, reindexing,
or repairing rows.

| State | Condition | Expected |
| --- | --- | --- |
| `legacy_absent` | No `compiler_dependencies` block and no dependency-overlay evidence | valid |
| `off` | exact `mode=off` / `enabled=false` / `n_facts=0` / `n_translation_units=0` | zero dependency-overlay relationships |
| `enabled` | `mode=compiler_m` + `enabled=true` | full relationship + manifest census |

A present `compiler_dependencies` key that is null, a list, a string, or any
other non-object is invalid, not legacy. Extra or missing enabled-block keys,
inconsistent enablement, evidence without a block, an enabled block without
the declared relationships, and an off block with dependency evidence are
violations. Relationship checks: unique relationship IDs, complete
identity (`type=depends_on` + `fact_kind=translation_unit_dependency` +
`extractor=c-compiler-deps`), the exact producer payload (including nullable
`compiler_id` present as null), file-entity endpoints, source-file agreement,
deterministic producer IDs, exact description, `weight=1.0` /
`confidence=1.0` / `is_deterministic=true`, empty span and metadata lists,
digest agreement, and preprocessor provenance
`["compiler_configuration_dependency"]`. `compiler_includes` relationships
are not dependency carriers. Manifest checks: exact producer key set,
canonical compiler census, singular/multi-compiler agreement,
`n_facts ==` decorated relationship count, sorted unique
`translation_unit_titles` that name persisted file entities (a configured TU
may have zero package-local edges), and pinned confidence-boundary text. The
graph-root entry point SHA-256-fingerprints `manifest.json`, the three
parquet tables, the `current` pointer, and the snapshot directory listing.
`--output` must be outside the audited graph root.
`published_graph_health.py` attaches this status as
`compiler_dependency_integrity` for C graphs only.

#### Direct include hierarchy (`includes`)

When `scripts/index_c.py --compiler-includes` is enabled (default **off**), the
graph gains **direct** file→file `includes` edges from GNU/Clang
`compiler -E -H` traces for each compile-database entry. Hierarchy is rebuilt
from the full depth stack (including outside/system frames) before filtering to
indexed package-local endpoints, so children are not re-parented incorrectly.

| Field | Value |
| --- | --- |
| `type` | `includes` |
| `source` / `target` | File-entity titles of the **including** and **directly included** files |
| `fact_kind` | `configured_direct_include` |
| `extractor` | `c-compiler-includes` |
| `confidence` / `is_deterministic` | `1.0` / `true` **only** relative to the recorded toolchain + compile DB |
| Description | Compiler/configuration-derived **direct** include in the active hierarchy (not flattened) |

Example: if `main.c` includes `direct.h` and `direct.h` includes `transitive.h`,
the include overlay emits `main.c → direct.h` and `direct.h → transitive.h`,
**not** `main.c → transitive.h`. The depends_on overlay may still emit the
flattened `main.c → transitive.h` edge.

**Persisted integrity audit (read-only):**
`scripts/c_compiler_include_graph_audit.py` validates already-published
direct `includes` relationships against the producer contract without
invoking a compiler, reading `compile_commands.json`, reading C/header
sources, running `compiler -E -H`, reconstructing include hierarchies,
reindexing, or repairing rows.

| State | Condition | Expected |
| --- | --- | --- |
| `legacy_absent` | No `compiler_includes` block and no include-overlay evidence | valid |
| `off` | exact `mode=off` / `enabled=false` / `n_facts=0` / `n_translation_units=0` | zero include-overlay relationships |
| `enabled` | `mode=compiler_eh` + `enabled=true` | full relationship + manifest census |

A present `compiler_includes` key that is null, a list, a string, or any
other non-object is invalid, not legacy. Extra or missing enabled-block keys,
inconsistent enablement, evidence without a block, an enabled block without
the declared relationships, and an off block with include evidence are
violations. Relationship checks: unique relationship IDs, complete identity
(`type=includes` + `fact_kind=configured_direct_include` +
`extractor=c-compiler-includes`), the exact producer payload (including
nullable `compiler_id` present as null), file-entity endpoints (header →
header is valid; sources need not all be TUs), source-file agreement,
deterministic producer IDs (digest includes `FACT_KIND`), exact description,
`weight=1.0` / `confidence=1.0` / `is_deterministic=true`, empty span and
metadata lists, digest agreement, and preprocessor provenance
`["compiler_configuration_direct_include"]`. Flattened `depends_on` /
`compiler_dependencies` rows are not include carriers. Manifest checks:
exact producer key set, canonical compiler census, singular/multi-compiler
agreement, compiler/TU censuses that do not exceed `n_compile_entries`,
`n_facts ==` decorated include count, sorted unique
`translation_unit_titles` that name persisted file entities (a configured TU
may have zero package-local include edges), and pinned confidence-boundary
text. The graph-root entry point SHA-256-fingerprints `manifest.json`, the
three parquet tables, the `current` pointer, and the snapshot directory
listing. `--output` must be outside the audited graph root.
`published_graph_health.py` attaches this status as
`compiler_include_integrity` for C graphs only.

#### Configured function signatures (entity fields, not edges)

When `scripts/index_c.py --clang-signatures` is enabled (default **off**),
existing tree-sitter-c **function** entities that the standalone
`scripts/c_clang_ast_audit.py` report classifies as `matched` with
`line_column_confirmed=true` gain configuration/toolchain-derived Clang
signature columns (`clang_qual_type`, storage/inline/variadic/mangled metadata,
compiler/digest provenance, canonical `clang_signature_observations_json`).

| Property | Value |
| --- | --- |
| Graph shape | **No** new entities or relationships; entity count unchanged |
| Attachment key | Collision-safe `tree_sitter_title` + package-relative source path |
| `clang_signature_fact_kind` | `configured_function_signature` |
| `clang_signature_extractor` | `clang-ast-json` |
| Confidence | `clang_signature_confidence=1.0` only relative to recorded Clang + compile DB |
| Fail-closed residuals | Any `clang_only` / `ambiguous` / `macro_location_unsupported` / unconfirmed location / missing entity aborts the overlay |
| Allowed residuals | `tree_sitter_only` and `out_of_compile_db_scope` remain counted in the manifest and receive **no** invented signatures |

Base tree-sitter `extractor` / `confidence` / `is_deterministic` / title / id
are unchanged. The standalone audit remains a diagnostic CLI; only this flag
publishes selected matched metadata into BYOG.

**Persisted integrity audit (read-only):**
`scripts/c_clang_signature_graph_audit.py` validates already-published
function-signature entity fields against the producer contract without
invoking Clang, reading `compile_commands.json`, building an AST capture,
reindexing, or re-running the overlay. It never repairs data.

| State | Condition | Expected |
| --- | --- | --- |
| `legacy_absent` | No `clang_signatures` block and no signature fields | valid |
| `off` | `mode=off` and `enabled=false` | zero signature fields required |
| `enabled` | `mode=clang_ast_signatures` + `enabled=true` | full entity + manifest census |

A present `clang_signatures` key that is null, a list, a string, or any other
non-object is invalid, not legacy. A missing block never legitimizes existing
signature fields; `mode=off` with signature fields is a violation; an enabled
block with partial, corrupted, or extra fields is a violation. Entity checks:
unique entity IDs, function type only, the exact full set of known producer
keys (including nullable `clang_storage_class` / `clang_inline` /
`clang_variadic` / `clang_mangled_name`) with no unknown material
`clang_signature_*` fields, `clang_signature_status=matched`, pinned
`fact_kind` / `extractor` / `confidence=1.0` / `is_deterministic=true`,
non-empty `clang_qual_type` and `clang_location_origin`, absolute compiler
path, nonempty compiler id and digest, sorted unique `entry_indices` inside
the manifest compile-entry census, `clang_signature_observations_json` as
**canonical deterministic JSON** (NaN, Infinity, duplicate object keys and
non-canonical encodings are refused), observation/entity/manifest agreement
on qualType, digest, compiler identities and entry-index union, and an
exact producer-owned description. Manifest checks: mode/enabled,
`fact_kind` / `extractor`, `n_facts == counts.matched ==` the actual
decorated-entity count, `n_facts_changed` in `[0, n_facts]`, fail-closed
`clang_only` / `ambiguous` / `macro_location_unsupported` counts zero,
non-negative `tree_sitter_only` / `out_of_compile_db_scope` counts, a
valid compile-entry/translation-unit census, non-empty unique internally
consistent compiler provenance, a non-empty digest, and the pinned
confidence-boundary text. The graph-root entry point SHA-256-fingerprints
`manifest.json`, the three parquet tables, the `current` pointer, and the
snapshot directory listing before and after the audit. Exit 0 = passed,
1 = violations, 2 = unreadable graph/snapshot/manifest.
`published_graph_health.py` attaches this status as
`clang_signature_integrity` for C published graphs; Python graphs are
unchanged. `--output` must be outside the audited graph root.

#### Configured direct-call evidence (relationship fields, not new edges)

When `scripts/index_c.py --clang-calls` is enabled (default **off**), existing
tree-sitter-c **`calls`** relationships that the standalone
`scripts/c_clang_call_audit.py` report classifies as `matched_internal` gain
configuration/toolchain-derived Clang call-evidence columns
(`clang_call_status`, `clang_call_match_basis`, `clang_call_byte_offset`,
entry indices, compiler/digest provenance, canonical
`clang_call_observations_json` / `clang_call_compilers_json`).

| Property | Value |
| --- | --- |
| Graph shape | **No** new entities or relationships; relationship IDs/endpoints/types unchanged |
| Attachment key | `type=calls` + caller/target titles + package-relative `source_file` + exact `tree_sitter_span` + exact derived byte offset |
| `clang_call_fact_kind` | `configured_direct_call` |
| `clang_call_extractor` | `clang-ast-json` |
| Confidence | `clang_call_confidence=1.0` only relative to recorded Clang + compile DB; base relationship `confidence=0.9` / `extractor=tree-sitter-c` stay unchanged |
| Fail-closed residuals | Any `clang_only_internal` / `ambiguous` / `macro_location_unsupported` / `covered_by_noninternal_clang_observation`, malformed compiler/digest/entry provenance, missing/duplicate relationship match, or byte-offset mismatch aborts before mutation |
| Allowed residuals | `tree_sitter_only_internal`, `out_of_compile_db_scope`, `external_direct`, and `indirect` remain counted in the manifest and receive **no** invented call metadata |

The audit is byte-offset-first and may use strict line/column fallback when
Clang omits its offset. Publication still requires the tree-sitter source span
to derive the exact relationship byte offset; column-only attachment is
impossible. This is **not** points-to analysis, macro-complete call proof,
multi-config coverage, C++, or ABI verification. The standalone call audit
remains a diagnostic CLI; only this flag publishes selected matched metadata
into BYOG.

**Persisted integrity audit (read-only):**
`scripts/c_clang_call_graph_audit.py` validates already-published
`clang_call_*` relationship fields against the producer contract without
invoking Clang, reading `compile_commands.json`, reading C sources,
reconstructing byte offsets, reindexing, or repairing rows.

| State | Condition | Expected |
| --- | --- | --- |
| `legacy_absent` | No `clang_calls` block and no `clang_call_*` fields | valid |
| `off` | `mode=off` and `enabled=false` | zero `clang_call_*` fields required |
| `enabled` | `mode=clang_configured_call_overlay` + `enabled=true` | full relationship + manifest census |

A present `clang_calls` key that is null, a list, a string, or any other
non-object is invalid, not legacy. Extra or missing enabled-block keys, extra
count/accounting keys, evidence without a block, an enabled block without the
declared evidence, and `mode=off` with call fields are violations. Relationship
checks: unique relationship IDs, `type=calls` only, the exact full producer key
set (including nullable `clang_call_resolve_reason` / `clang_call_ref_type` /
singular compiler fields), no unknown material `clang_call_*` fields, pinned
`status` / `fact_kind` / `extractor` / `confidence=1.0` /
`is_deterministic=true` / `ref_kind=FunctionDecl`, a valid match basis and
non-negative byte offset, sorted unique `entry_indices` inside the compile-entry
census, canonical `clang_call_compilers_json` and
`clang_call_observations_json`, observation/relationship/manifest agreement on
target, ref type, digest, compiler identities and entry-index coverage, and an
exact producer-owned description. Manifest checks: exact key set, `n_facts ==
counts.matched_internal ==` decorated count, fail-closed residuals zero
(including `covered_by_noninternal_clang_observation`), observation-only counts
non-negative and arithmetically consistent, `tree_sitter_accounting.total_calls`
equal to the actual `calls` relationship count, and pinned confidence-boundary
text. The graph-root entry point SHA-256-fingerprints `manifest.json`, the three
parquet tables, the `current` pointer, and the snapshot directory listing.
`--output` must be outside the audited graph root.
`published_graph_health.py` attaches this status as `clang_call_integrity` for C
graphs only.

#### Configured type-declaration evidence (entity fields, not a type graph)

When `scripts/index_c.py --clang-types` is enabled (default **off**), existing
tree-sitter-c **`struct` / `enum` / `typedef`** entities that the standalone
`scripts/c_clang_type_audit.py` report classifies as `matched` gain
configuration/toolchain-derived Clang type-declaration columns under the
`clang_type_*` namespace.

| Property | Value |
| --- | --- |
| Graph shape | **No** new entities or relationships; entity count unchanged; **no** `uses_type` edges |
| Attachment key | Exact `tree_sitter_title` + entity `type`/`entity_kind` + `symbol_name` + package-relative source path + **canonical graph `span`** |
| Canonical vs matched site | Entity keeps its graph-canonical span; fields record both `clang_type_graph_canonical_*` and `clang_type_matched_site_*` (may differ, e.g. `ini_handler` graph line 58 vs matched line 62) |
| `clang_type_fact_kind` | `configured_type_declaration` |
| `clang_type_extractor` | `clang-ast-json` |
| Confidence | `clang_type_confidence=1.0` / `clang_type_is_deterministic=true` only relative to recorded Clang + compile DB |
| Fail-closed residuals | Any `tree_sitter_only` / `clang_only` / `ambiguous` / `macro_location_unsupported`, type/path/span/title mismatch, non-unique title, stale `clang_type_*`, or provenance disagreement aborts before mutation |
| Allowed residuals | `out_of_compile_db_scope`, `anonymous_declarations`, `unsupported_declarations`, `outside_package_declarations`, and `alternate_declaration_sites` remain counted in the manifest and receive **no** invented entities |

Optional type strings (`clang_type_qual_type`, `clang_type_desugared_qual_type`,
`clang_type_fixed_underlying_type`) are set to null when Clang does not supply
them. Singular `clang_type_compiler_path` / `clang_type_compiler_id` are set
only for single-compiler rows; multi-compiler rows keep the canonical
`clang_type_compilers` JSON list only. Base tree-sitter `title` / `id` /
`type` / `source_file` / `span` / `snippet` / `extractor` / `confidence` /
`is_deterministic` / text-unit IDs are never rewritten. Alternate declaration
sites are never published as separate entities.

**Persisted integrity audit (read-only):**
`scripts/c_clang_type_graph_audit.py` validates already-published
`clang_type_*` entity fields against the producer contract without invoking
Clang, reading `compile_commands.json`, building an AST capture, reindexing, or
re-running the overlay. It never repairs data.

| State | Condition | Expected |
| --- | --- | --- |
| `legacy_absent` | No `clang_types` block and no `clang_type_*` fields | valid |
| `off` | `mode=off` and `enabled=false` | zero `clang_type_*` fields required |
| `enabled` | `mode=configured_clang_type_declarations` + `enabled=true` | full entity + manifest census |

A present `clang_types` key that is null, a list, a string, or any other
non-object is invalid, not legacy. A missing block in a readable manifest
never legitimizes existing type fields; `mode=off` with type fields is a
violation; an enabled block with partial, corrupted, or extra fields is a
violation. Entity checks: unique entity IDs, `struct` / `enum` / `typedef`
type only, the exact full set of known `clang_type_*` fields with no unknown
material ones, `clang_type_declaration_confirmed=true`,
`clang_type_entity_kind == entity.type`,
`clang_type_graph_canonical_span == entity.span`, pinned `fact_kind` /
`extractor` / `confidence=1.0` / `is_deterministic=true`, internally
consistent graph-canonical and matched-site span/line/col0, a boolean
`clang_type_matched_site_is_canonical` that equals the actual site equality,
optional type-string fields null or non-empty strings, a non-empty location
origin, sorted unique `entry_indices` inside the manifest compile-entry
census, `clang_type_compilers` as **canonical deterministic JSON** (NaN,
Infinity, duplicate object keys and non-canonical encodings are refused),
entity/manifest agreement on compiler identities and
`compile_commands_digest` (singular compiler fields only for a single
identity), and description text that keeps its evidence boundary and claims
no ABI/layout/type-use/multi-config/macro-complete proof. Manifest checks:
mode/enabled, `fact_kind` / `extractor`,
`n_facts == counts.matched ==` the actual decorated-entity count,
`n_facts_changed` in `[0, n_facts]`, all four fail-closed bucket counts
zero, non-negative observation-only counts, a valid
compile-entry/translation-unit census, non-empty unique internally consistent
compiler provenance, a non-empty digest, and the pinned confidence-boundary
text with no ABI/layout/type-use/multi-config/macro-complete guarantee. The
graph-root entry point SHA-256-fingerprints `manifest.json`, the three
parquet tables, the `current` pointer, and the snapshot directory listing
before and after the audit and publishes that read-only verification in its
report. Exit 0 = passed, 1 = violations, 2 = unreadable
graph/snapshot/manifest. `published_graph_health.py` attaches this status for
C published graphs; legacy/default-off roots continue to pass. The
deterministic JSON names `state`, `classification`, `violations`, counts,
compiler/configuration provenance, limitations and the read-only result;
`anomalies` remains an equivalent compatibility field. `--output` must be
outside the audited graph root.

#### Shared in-memory AST capture (execution only)

When any of `--clang-signatures` / `--clang-calls` / `--clang-types` /
`--clang-type-uses` / `--clang-type-shapes` are enabled, `index_c` creates one
in-process AST capture
(`scripts/c_clang_ast_capture.py`): load `compile_commands.json` once,
fail-closed validate arguments/compiler identity, and run one Clang
`-ast-dump=json` per compile entry. Function, call, type-declaration,
type-use, and type-shape builders consume that capture without re-invoking the
compiler (shared intermediate audits are reused when multiple overlays need
them: the type-declaration audit is built at most once and passed to both the
type-use and type-shape builders, which validate it against the same capture,
digest, and toolchain before reuse). Any non-empty
flag combination still costs **N** dumps for **N** entries (never 2N–5N). There
is **no** disk AST cache and AST roots never appear in manifests, parquet, or
logs. Standalone `c_clang_ast_audit.py` / `c_clang_call_audit.py` /
`c_clang_type_audit.py` / `c_clang_type_use_audit.py` /
`c_clang_type_shape_audit.py` CLIs remain available and
each still capture once for their own run, with byte-identical output whether
or not a precomputed report is reused. Confidence boundaries and
independent `clang_signatures` / `clang_calls` / `clang_types` /
`clang_type_uses` / `clang_type_shapes` manifest blocks are unchanged (no
combined capture block).

#### C symbol identity (tree-sitter extractor)

C symbol entities (`function` / `struct` / `enum` / `typedef`) share the
collision-safe module key policy documented above. **Cross-kind collision
within one module key:** when two or more of those kinds use the same bare C
name, every colliding kind is titled `module_key:entity_kind:name` (no
arbitrary legacy winner). Non-colliding symbols keep `module_key:name`.
Same-kind redeclarations are deduplicated by `(module_key, kind, name)` –
never by rendered title alone. Symbol entities also carry an authoritative
`symbol_name` field (bare C name) so consumers need not re-parse qualified
titles. `contains` relationship IDs stay `rel:contains:module_key:name` unless
the module/name pair is cross-kind colliding, in which case they use
`rel:contains:module_key:entity_kind:name`. Call edges still connect only
function entities and keep historical call IDs when no function participates
in a cross-kind collision.

**Typedef declarators:** alias names are resolved from each `type_definition`
`declarator` field by following only `pointer_declarator` /
`function_declarator` / `parenthesized_declarator` / `array_declarator`
/ `attributed_declarator` wrappers to the alias `type_identifier`. Parameter
identifiers, parameter type tags, and underlying type names are never treated
as aliases. Multiple top-level declarators in one typedef
(`typedef int a, *b, (*c)(void);`) each produce an alias.

**Multi-site type-audit matching (diagnostic only):** the graph still emits one
canonical entity per semantic key (first walk-order site). The Clang type
declaration audit collects every owned tree-sitter declaration site and matches
configured Clang only by exact
`entity_kind + path + name + line + col0`. A non-canonical exact site may match
without changing the graph span; unselected owned sites appear under
`alternate_declaration_sites` and do not fail `--fail-on-mismatch`.

#### Type-declaration audit (standalone diagnostic CLI)

`scripts/c_clang_type_audit.py` compares configured Clang type declarations
to tree-sitter-c type entities. **In scope for matching:** named complete
`struct` (`RecordDecl`), named complete `enum` (`EnumDecl`), package-local
`typedef` (`TypedefDecl`). **Explicit residual buckets (not matched):**
anonymous declarations, unions / incomplete / unsupported forms, and
outside-package/system declarations; also the usual
`tree_sitter_only` / `clang_only` / `ambiguous` / `macro_location_unsupported`
/ `out_of_compile_db_scope` classes, plus observation-only
`alternate_declaration_sites`. Identity is
`entity_kind + package-relative path + name + exact line + exact col0`
(never bare title alone; a struct and a typedef with the same title stay
distinct). `--fail-on-mismatch` exits 1 only when `tree_sitter_only`,
`clang_only`, `ambiguous`, or `macro_location_unsupported` is non-zero;
out-of-scope / anonymous / unsupported / outside-package / alternate sites
alone do not. Outside-package rows retain their resolved source path and are
deduplicated by that path; declarations from different system headers are
never collapsed just because their names and coordinates happen to agree.

The standalone CLI does not mutate BYOG. Publishing selected matched type
fields into the graph requires the separate explicit `--clang-types` flag
(see above). Default-off graphs still have no `uses_type` edges until
`--clang-type-uses` is enabled.

#### Type-use audit (standalone diagnostic CLI)

`scripts/c_clang_type_use_audit.py` inventories Clang-observed type uses on
declaration-bearing AST nodes (function returns, parameters, locals, fields,
globals, typedef underlying types). It reuses
`build_function_audit_from_capture` and
`build_type_declaration_audit_from_capture` for owner/target identity.

| Property | Value |
| --- | --- |
| Standalone CLI | Diagnostic only (no graph mutation by itself) |
| Location honesty | `location_precision=declaration_bearing_node` (loc / range.begin of the declaring node). **Not** claimed as an exact type-token span |
| Matched when | owner uniquely maps (when an owner context exists) and target uniquely maps to a package-local matched struct/enum/typedef |
| Target resolvers | `type_alias_decl_id` (scoped to one compile entry), `exact_tag_spelling`, `unique_typedef_spelling`; C's bare typedef namespace stays distinct from explicit `struct T` / `enum T` tags |
| Owner resolvers | Exact declaration site first; unique external function name for header prototypes; unique same-file static function name for forward declarations; owned anonymous-tag typedef site |
| Fail-closed residuals | `owner_unmatched`, `target_unresolved`, `ambiguous_target`, `macro_location_unsupported` |
| Observation-only | `external_or_system`, `unsupported_type_form`, `unowned_context` |

#### Configured type-use edges (`uses_type`)

When `scripts/index_c.py --clang-type-uses` is enabled (default **off**),
`matched_internal` rows are aggregated into `uses_type` relationships:

| Property | Value |
| --- | --- |
| Graph shape | New `uses_type` edges only; **no** new entities; recursive self-edges allowed |
| Aggregation | One edge per unique **source entity id + target entity id** |
| Observation vs edge | Diagnostic observation count ≥ edge count (multiple use kinds/sites merge) |
| `fact_kind` | `configured_type_use` |
| `extractor` | `clang-ast-json` |
| Confidence | `1.0` / deterministic only relative to recorded Clang + compile DB |
| Evidence | Sorted use kinds, observation count, canonical observations JSON, entry indices, compiler list/digest |
| Fail-closed | Same residual buckets as the type-use audit; missing/non-unique endpoints abort before mutation |
| Manifest | Independent `clang_type_uses` block (`mode=off` or `configured_clang_type_uses`) |

**Local query + context-pack consumption (when edges exist):**

| Surface | Behavior |
| --- | --- |
| `ByogGraph.types_used_by(symbol)` | Sorted unique outgoing `uses_type` target titles |
| `ByogGraph.type_users(symbol)` | Sorted unique incoming `uses_type` source titles |
| `ByogGraph.type_closure(symbol, …)` | Bounded cycle-safe BFS over **only** `uses_type` (directions: `dependencies` / `users` / `both`); min depths; self-edges as evidence without node duplication; caps truncate **returned** lists while `n_*_total` stay exact within `max_depth`; malformed rows or duplicate relationship IDs fail closed |
| CLI | `graph_query.py types-used-by` / `type-users` / `type-closure`; same via `graphrag_code.py` (delegation; human + `--json` parity); negative limits / bad directions / malformed `uses_type` rows exit non-zero |
| Context pack (outgoing, depth 1) | `type_dependencies` + `type_dependency_edges` (+ totals/truncated) |
| Context pack (incoming, depth 1) | `type_user_edges` (+ totals/truncated) |
| Context pack (depth > 1) | Adds `type_dependency_closure` / `type_user_closure` with per-node min depth, bounded entity text, compact edge evidence, exact totals and truncation flags; default `--type-depth 1` keeps pack JSON byte-identical to direct-only; dangling or non-unique entity endpoints retain one explicit `missing` / `ambiguous` node payload rather than falsifying returned counts |
| Evidence bounding | Defaults: 20 edges/direction (also 20 returned closure-node payloads when depth > 1), 5 observations/edge; sample + truncation counts; no unbounded raw observations JSON; malformed legacy JSON and declared/decoded count disagreements are surfaced without invented samples |
| Neighbor cap | Type sections are built from the full relationship set, not the capped 30-neighbor list |

Call-graph queries (`callers` / `callees` / `impact` / `dependency_order`) never
traverse `uses_type`. Closure never traverses `calls`, `contains`,
`depends_on`, `includes`, or `uses_data`. Graphs without `uses_type` edges
return empty query lists and omit the type_* pack keys (byte-identical pack
shape at default depth aside from docs/pins).

**Persisted integrity audit (read-only):**
`scripts/c_clang_type_use_graph_audit.py` validates already-published
configured `uses_type` edges against the producer contract without invoking
Clang or re-running the overlay.

| Mode | Condition | Expected |
| --- | --- | --- |
| `legacy_absent` | No `clang_type_uses` block and zero configured edges | valid |
| `off` | `mode=off` and `enabled=false` | zero configured edges required |
| `enabled` | `mode=configured_clang_type_uses` + `enabled=true` | full edge + manifest census |

A missing block in a readable manifest never legitimizes existing configured
edges; a missing or malformed snapshot manifest is an I/O failure, not a
legacy graph. Checks include
unique relationship IDs, no stale `clang_type_use_*` on non-`uses_type` rows,
mirrored fact/extractor/confidence fields, collision-safe title→entity
resolution, stored entity-id agreement, deterministic `relationship_id`,
one edge per source/target entity-id pair (self-edges allowed), strict
observations JSON (canonical order, count, use_kinds, entry_indices),
compiler/digest consistency, unknown material fields fail-closed, and
manifest `n_facts` / `n_observations` cross-checks. Bounded anomaly samples
retain exact totals. `published_graph_health.py` attaches this status for C
published graphs; legacy/default-off roots continue to pass.

This is configuration-derived type-use *evidence* – not layout/ABI proof,
multi-config coverage, or points-to analysis.

#### Type-shape audit and optional `--clang-type-shapes` overlay

`scripts/c_clang_type_shape_audit.py` inventories **direct** struct fields and
enum enumerators for owners already matched by the type-declaration audit.
Hard equality is **ordered member names only**. Clang type spellings, enum
integer values, and bit-field widths are residual evidence fields – never
size, alignment, offsets, calling convention, or Rust/FFI representation
claims. Typedef aliases are not independent shapes. Nested record bodies are
not flattened into parents. The audit CLI itself produces no graph entities,
relationships, or manifest blocks.

`--clang-type-shapes` (default off) publishes that evidence as `clang_shape_*`
fields on **existing** tree-sitter `struct` / `enum` entities, using only
`matched_shape` rows from the same builder (no second AST traversal, matcher,
or compiler invocation). The type-declaration audit that supplies the owners is
built once and passed in, so with `--clang-types` enabled as well it is not
rebuilt.

| Field | Meaning |
| --- | --- |
| `clang_shape_members_validated` | `true` only for a validated `matched_shape` row |
| `clang_shape_fact_kind` / `clang_shape_extractor` | `configured_type_shape` / `clang-ast-json` |
| `clang_shape_entity_kind` | `struct` or `enum` (matches the graph entity type) |
| `clang_shape_member_count` | Number of ordered direct members |
| `clang_shape_member_names` | Deterministic canonical JSON array of ordered direct member names (**the only hard equality**) |
| `clang_shape_member_evidence` | Deterministic canonical JSON of per-member `qualType`, `desugaredQualType`, `enum_value`, `bit_width`, `is_bitfield`, `form`, `line`, `col0` – diagnostic evidence only |
| `clang_shape_graph_canonical_span` | Canonical graph span of the decorated entity (unchanged by the overlay) |
| `clang_shape_matched_site_span` / `_line` / `_col0` / `_is_canonical` | Configured declaration site actually matched |
| `clang_shape_location_origin`, `clang_shape_entry_indices` | Location provenance and contributing compile entries |
| `clang_shape_compiler_path` / `_id` / `_compilers` / `_compile_commands_digest` | Toolchain and configuration identity |
| `clang_shape_confidence` / `clang_shape_is_deterministic` / `clang_shape_description` | Configuration-relative determinism boundary |

Attachment is collision-safe and fail-closed: a row applies only when exactly
one graph entity agrees on entity type, exact `tree_sitter_title`,
`symbol_name`, package-relative source path, and canonical graph span, and only
when the shape row and its type-declaration owner agree on kind, name, path,
and matched site. Missing, non-unique, or diverging targets abort the whole
overlay before any entity is touched (plan-then-mutate atomicity). Base entity
IDs, titles, spans, `confidence`, `extractor`, and tree-sitter fields are never
rewritten, and no entities, relationships, `uses_type` edges, alternate-site
entities, or ABI/layout facts are created.

Fail-closed residuals: `tree_sitter_only_members`, `clang_only_members`,
`member_order_mismatch`, `duplicate_or_ambiguous_members`,
`macro_location_unsupported`, `owner_unmatched`. Observation-only:
`unsupported_member_form`, `outside_package_declarations` – they never create
metadata and never abort the overlay, but their counts are recorded in the
`clang_type_shapes` manifest block together with compiler identity, digest,
compile-entry count, every bucket count, the number of decorated entities, the
explicit confidence boundary, and the limitations (not ABI, not layout, not FFI
proof, not Rust `repr` proof, not multi-config, not C++).

**Persisted integrity audit (read-only):**
`scripts/c_clang_type_shape_graph_audit.py` validates already-published
`clang_shape_*` entity fields against the producer contract without invoking
Clang, reading `compile_commands.json`, building an AST capture, reindexing, or
re-running the overlay. It never repairs data.

| State | Condition | Expected |
| --- | --- | --- |
| `legacy_absent` | No `clang_type_shapes` block and no `clang_shape_*` fields | valid |
| `off` | `mode=off` and `enabled=false` | zero `clang_shape_*` fields required |
| `enabled` | `mode=configured_clang_type_shapes` + `enabled=true` | full entity + manifest census |

A missing block in a readable manifest never legitimizes existing shape fields;
`mode=off` with shape fields is a violation; an enabled block with partial,
corrupted, or extra fields is a violation. Entity checks: unique entity IDs,
`struct` / `enum` type only, the exact full set of known `clang_shape_*` fields
with no unknown material ones, `clang_shape_members_validated=true`,
`clang_shape_entity_kind == entity.type`,
`clang_shape_graph_canonical_span == entity.span`, pinned `fact_kind` /
`extractor` / `confidence=1.0` / `is_deterministic=true`, well-typed
matched-site line/column/span, a non-negative member count, member names and
member evidence that decode as **canonical deterministic JSON** (NaN, Infinity,
duplicate object keys and non-canonical encodings are refused), name/evidence
census equal to the member count, non-empty unique names, per-member
`order == position` with a matching name, no residual member forms, boolean
`is_bitfield`, integer-or-null bit widths and enum values, sorted unique
`entry_indices` inside the manifest compile-entry census, entity/manifest
agreement on compiler identities and `compile_commands_digest` (singular
compiler fields only for a single identity), and description text that keeps
its evidence boundary and claims no ABI/layout/FFI/`repr` proof. Manifest
checks: mode/enabled, `fact_kind` / `extractor`,
`n_facts == n_decorated_entities == counts.matched_shape ==` the actual
decorated-entity count, all six fail-closed bucket counts zero, non-negative
observation-only counts, an internally consistent owner census, a valid
compile-entry/translation-unit census, non-empty unique internally consistent
compiler provenance, a non-empty digest, `hard_equality` equal to
`ordered direct member names only`, and the pinned `evidence_only`,
`limitations`, and confidence-boundary text with no ABI/layout/FFI/`repr`/
multi-config/C++ guarantee. The graph-root entry point SHA-256-fingerprints
`manifest.json`, the three parquet tables, the `current` pointer, and the
snapshot directory listing before and after the audit and publishes that
read-only verification in its report. Exit 0 = passed, 1 = violations,
2 = unreadable graph/snapshot/manifest. `published_graph_health.py` attaches
this status for C published graphs; legacy/default-off roots continue to pass.
The deterministic JSON names `state`, `classification`, `violations`, counts,
compiler/configuration provenance, limitations and the read-only result;
`anomalies` remains an equivalent compatibility field. `--output` must be
outside the audited graph root.

**Persisted preprocessor-liveness integrity (read-only):**
`scripts/c_preprocessor_liveness_graph_audit.py` validates already-published
`preprocessor_*` row stamps and the `preprocessor_liveness` manifest block
against the producer contract without invoking a compiler, reading
`compile_commands.json`, reading C/header sources, reconstructing macro
tables or branch decisions, comparing the recorded digest with the current
host, reindexing, or repairing rows.

| State | Condition | Expected |
| --- | --- | --- |
| `legacy_absent` | No `preprocessor_liveness` block and no material stamp evidence | valid |
| `no_compiler` | `eval_mode=no_compiler`, host-independent, zero builtin/include census | full five-field stamps on entities, base relationships, and observations |
| `compiler_builtins` | `eval_mode=compiler_builtins`, host-specific, positive builtin census | full five-field stamps matching the recorded digest |

A present `preprocessor_liveness` key that is null, a list, a string, or any
other non-object is invalid, not legacy. Manifest absence never legitimizes
material eval-mode/digest/branch stamps. Row checks: all five producer keys
present, `preprocessor_dependent` is a strict boolean equal to
`bool(preprocessor_reasons)`, unique canonical reason families, exact branch
objects (`kind` / `condition` / `start_line` / `end_line` / `liveness` /
`basis`) with compatible `branch_<liveness>:<kind>(...)` reasons, and stamp
mode/digest agreement with the manifest. The publisher's `counts.entities`,
`counts.relationships`, and `counts.call_observations` census is mandatory
and must agree with the loaded tables (including absence of the optional
observation parquet). Complete post-annotation overlay
identities (`depends_on` / `includes` / configured `uses_type`) are exempt
from the five-field stamp; partial overlay markers are not. The graph-root
entry point SHA-256-fingerprints `manifest.json`, the parquet tables
including optional `call_observations.parquet`, the `current` pointer, and
the snapshot directory listing. `--output` must be outside the audited
graph root. `published_graph_health.py` attaches this status as
`preprocessor_liveness_integrity` for C graphs only.

**Persisted overlay coherence (read-only):**
`scripts/c_overlay_coherence_graph_audit.py` reuses the seven compiler-backed
component validators and additionally requires every enabled subset to share
one compile-database digest, compile-entry count, and normalized compiler
census (`compiler_path` / `compiler_id` / `compiler_version`). When both
`compiler_dependencies` and `compiler_includes` are enabled their
translation-unit titles and counts must also agree. Off and legacy-absent
blocks do not participate. `preprocessor_liveness` remains an aggregate
integrity component, but its provenance is reported independently and is
never compared with overlay identities.

| State | Condition | Expected |
| --- | --- | --- |
| `legacy_absent` | All seven compiler-backed blocks absent, no carrier evidence | valid |
| `off` | Every present compiler-backed overlay is `off` | valid |
| `coherent` | One or more overlays enabled and shared fields agree | valid |
| `invalid` | Component integrity failure or cross-overlay mismatch | fail |

A snapshot can have independently valid enabled blocks that still fail
coherence when they were produced from different compile databases or
compiler captures. The audit proves persisted configuration agreement, not
correctness against live sources. `published_graph_health.py` attaches
`c_overlay_coherence_integrity` for C graphs only.

**Shared out of scope:** multi-config coverage, MSVC/wrappers/response files
(fail closed), system/outside endpoints after filtering, production C/C++
completeness. Snapshot manifests record separate `preprocessor_liveness`,
`compiler_dependencies`,
`compiler_includes`, `clang_signatures`, `clang_calls`, `clang_types`,
`clang_type_uses`, and `clang_type_shapes` blocks with their applicable mode, compiler
identity/identities, digest, fact/observation/TU counts, and residual counts;
all carry an explicit `mode=off` block when disabled. Module/cache/plugin/PCH, response/config, and unrestricted
`-Xclang` compile arguments fail closed for all compiler-backed adapters
until their effects can be audited without changing configured semantics.

## Example Row (entities)
```json
{
  "id": "ent:fn:physics.update_player",
  "title": "update_player",
  "type": "function",
  "description": "Advances one physics tick. Applies jump/gravity/horizontal and checks collisions.",
  "text_unit_ids": ["tu:sim:42-67"],
  "source_file": "examples/mini_game/physics.py",
  "span": "18:0-35:10",
  "extractor": "tree-sitter-python",
  "confidence": 1.0,
  "is_deterministic": true
}
```

## Usage in Phase 0+
1. Parser (tree-sitter + semantic) → normalized in-memory graph or intermediate records.
2. Serializer → exactly the three BYOG parquets (with our extra columns).
3. `graphrag index --root <proj>` with `workflows: [create_communities, create_community_reports, ...]`
4. Query layer consumes the resulting communities + reports + original parquets.

## Early Decisions (2026-06-15)
- Weight on relationships is mandatory for Leiden (per GraphRAG docs).
- Keep a parallel normalized NetworkX / DuckDB view for fast custom traversals ("callers of X", impact analysis) while the parquet files remain the source of truth for GraphRAG.
- All uncertain dynamic calls / macros / templates must be emitted with confidence < 1.0 and `is_deterministic=false`.

Update this doc as the parser and context-pack requirements evolve.
