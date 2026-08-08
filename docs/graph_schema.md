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

**Shared out of scope:** multi-config coverage, MSVC/wrappers/response files
(fail closed), system/outside endpoints after filtering, production C/C++
completeness. Snapshot manifests record separate `compiler_dependencies` and
`compiler_includes` blocks (`mode`, compiler identity/identities, digest,
fact/TU counts); both carry an explicit `mode=off` block when disabled.
Module-producing compiler flags also fail closed for the `-H` adapter until
their cache/output paths can be redirected without changing configured
semantics.

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
