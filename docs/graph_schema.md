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
- `confidence`: float [0,1] — 1.0 for deterministic parser facts
- `is_deterministic`: bool — true when the fact can be re-derived from source without LLM

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

#### Shared in-memory AST capture (execution only)

When any of `--clang-signatures` / `--clang-calls` / `--clang-types` /
`--clang-type-uses` are enabled, `index_c` creates one in-process AST capture
(`scripts/c_clang_ast_capture.py`): load `compile_commands.json` once,
fail-closed validate arguments/compiler identity, and run one Clang
`-ast-dump=json` per compile entry. Function, call, type-declaration, and
type-use builders consume that capture without re-invoking the compiler (shared
intermediate audits are reused when multiple overlays need them). Any non-empty
flag combination still costs **N** dumps for **N** entries (never 2N–4N). There
is **no** disk AST cache and AST roots never appear in manifests, parquet, or
logs. Standalone `c_clang_ast_audit.py` / `c_clang_call_audit.py` /
`c_clang_type_audit.py` / `c_clang_type_use_audit.py` CLIs remain available and
each still capture once for their own run. Confidence boundaries and
independent `clang_signatures` / `clang_calls` / `clang_types` /
`clang_type_uses` manifest blocks are unchanged (no combined capture block).

#### C symbol identity (tree-sitter extractor)

C symbol entities (`function` / `struct` / `enum` / `typedef`) share the
collision-safe module key policy documented above. **Cross-kind collision
within one module key:** when two or more of those kinds use the same bare C
name, every colliding kind is titled `module_key:entity_kind:name` (no
arbitrary legacy winner). Non-colliding symbols keep `module_key:name`.
Same-kind redeclarations are deduplicated by `(module_key, kind, name)` —
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

This is configuration-derived type-use *evidence* — not layout/ABI proof,
multi-config coverage, or points-to analysis.

**Shared out of scope:** multi-config coverage, MSVC/wrappers/response files
(fail closed), system/outside endpoints after filtering, production C/C++
completeness. Snapshot manifests record separate `compiler_dependencies`,
`compiler_includes`, `clang_signatures`, `clang_calls`, `clang_types`, and
`clang_type_uses` blocks with their applicable mode, compiler
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
