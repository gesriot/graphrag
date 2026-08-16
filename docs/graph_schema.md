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
its own staging directory and writer-lock file. The writer lease is
still held while the publisher waits for the graph-root exclusive
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
