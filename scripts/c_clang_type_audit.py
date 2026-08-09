#!/usr/bin/env python
"""Clang AST JSON type-declaration audit (diagnostic only).

Compares package-local Clang type *declarations* (named complete structs,
named complete enums, and package-local typedefs) against tree-sitter-c
``struct`` / ``enum`` / ``typedef`` entities.

This module is a **diagnostic audit** (and the pure builder consumed by the
optional ``--clang-types`` overlay). The standalone CLI does not mutate BYOG
and does not add ``uses_type`` edges.

Identity is collision-safe and includes entity kind, name, package-relative
path, and exact source start line/column (never bare title alone). A struct
and a typedef sharing a title remain distinct identities.

Clang only. Reuses the shared in-memory AST capture.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from c_clang_ast_audit import (  # type: ignore
    ClangAstAuditError,
    _normalize_path_token,
    resolve_function_location,
)
from c_compiler_common import path_is_under  # type: ignore
from c_identities import package_relative_posix  # type: ignore
from extract_c import (  # type: ignore
    TypeDeclarationSite,
    collect_type_declaration_sites,
)

MODE = "clang_ast_json_type_declaration_audit"
CONFIDENCE_BOUNDARY = (
    "Classifications are configuration/toolchain-derived declaration evidence "
    "from the recorded Clang + compile_commands.json only. This is not a type "
    "graph, not type-use / uses_type proof, not layout or ABI verification, "
    "not macro-complete fidelity, not multi-config coverage, and not C++. "
    "The graph keeps one canonical source-derived representative per semantic "
    "entity; the audit may match any exact tree-sitter declaration site owned "
    "by that entity. Alternate sites are declaration-site observations only "
    "(not proven dead/inactive). Anonymous, union, incomplete, unsupported, "
    "and outside-package observations remain explicit residuals. The standalone "
    "CLI publishes no type facts; optional ``--clang-types`` may attach matched "
    "fields only under a separate fail-closed overlay."
)

_SPAN_RE = re.compile(
    r"^(?P<sl>\d+):(?P<sc>\d+)-(?P<el>\d+):(?P<ec>\d+)$"
)

# Buckets that participate in --fail-on-mismatch exit 1.
_FAIL_ON_MISMATCH_BUCKETS = (
    "tree_sitter_only",
    "clang_only",
    "ambiguous",
    "macro_location_unsupported",
)

# Observation-only residuals (do not fail --fail-on-mismatch by themselves).
_OBSERVATION_ONLY_BUCKETS = (
    "out_of_compile_db_scope",
    "anonymous_declarations",
    "unsupported_declarations",
    "outside_package_declarations",
    "alternate_declaration_sites",
)

_ALL_BUCKETS = (
    "matched",
    "tree_sitter_only",
    "out_of_compile_db_scope",
    "clang_only",
    "ambiguous",
    "macro_location_unsupported",
    "anonymous_declarations",
    "unsupported_declarations",
    "outside_package_declarations",
    "alternate_declaration_sites",
)

EntityKind = str  # "struct" | "enum" | "typedef"


class ClangTypeAuditError(ClangAstAuditError):
    """Raised when the type-declaration audit cannot run honestly."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class ClangTypeDeclaration:
    """One Clang type declaration observation (may be package-local or not)."""

    entity_kind: EntityKind  # struct | enum | typedef | unknown
    name: Optional[str]
    source_path: Optional[str]  # package-relative when package-local
    line: Optional[int]  # 1-based
    col0: Optional[int]  # 0-based (normalized from Clang 1-based)
    clang_col1: Optional[int]  # raw Clang column when present
    location_origin: str
    spelling_path: Optional[str] = None
    expansion_path: Optional[str] = None
    is_package_local: bool = False
    is_anonymous: bool = False
    is_union: bool = False
    is_complete: Optional[bool] = None
    tag_kind: Optional[str] = None  # struct | union | None
    qual_type: Optional[str] = None
    desugared_qual_type: Optional[str] = None
    fixed_underlying_type: Optional[str] = None
    classification_hint: Optional[str] = None
    # outside_package | unsupported | macro_location_unsupported | ...
    entry_indices: List[int] = field(default_factory=list)
    compiler_path: Optional[str] = None
    compiler_id: Optional[str] = None
    compile_commands_digest: Optional[str] = None
    observation_variants: List[Dict[str, Any]] = field(default_factory=list)

    def matchable_identity(
        self,
    ) -> Optional[Tuple[str, str, str, int, int]]:
        """(kind, path, name, line, col0) when fully located and matchable."""
        if (
            self.entity_kind not in {"struct", "enum", "typedef"}
            or not self.name
            or not self.source_path
            or self.line is None
            or self.col0 is None
            or not self.is_package_local
            or self.classification_hint
            in {
                "macro_location_unsupported",
                "outside_package",
                "unsupported",
                "anonymous",
            }
        ):
            return None
        return (
            self.entity_kind,
            self.source_path,
            self.name,
            int(self.line),
            int(self.col0),
        )

    def merge_key(self) -> Tuple[Any, ...]:
        return (
            self.entity_kind,
            self.source_path or "",
            self.name or "",
            self.line if self.line is not None else -1,
            self.col0 if self.col0 is not None else -1,
            self.is_anonymous,
            self.is_union,
        )


@dataclass(frozen=True)
class TreeSitterTypeEntity:
    """One tree-sitter type declaration site owned by a semantic graph entity.

    Multiple sites may share the same ``title`` / semantic key when the
    extractor saw several declaration sites (e.g. ``#if`` / ``#else``).
    ``is_canonical`` marks the graph's published representative span.
    """

    title: str
    entity_kind: EntityKind
    name: str
    source_path: str
    line: int
    col0: int
    span: str = ""
    is_canonical: bool = True
    preprocessor_dependent: bool = False
    preprocessor_reasons: Tuple[str, ...] = ()
    preprocessor_branches: Tuple[Any, ...] = ()

    def identity(self) -> Tuple[str, str, str, int, int]:
        return (
            self.entity_kind,
            self.source_path,
            self.name,
            int(self.line),
            int(self.col0),
        )

    def semantic_key(self) -> Tuple[str, str, str]:
        return (self.entity_kind, self.source_path, self.name)


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------


def _clang_col_to_zero_based(col: Optional[int]) -> Optional[int]:
    """Clang AST JSON columns are 1-based; tree-sitter spans use 0-based cols."""
    if isinstance(col, bool) or not isinstance(col, int):
        return None
    if col < 1:
        return None
    return col - 1


def _positive_ast_int(value: Any) -> Optional[int]:
    """Accept only a positive JSON integer (never bool/string coercions)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _source_line_col_from_offset(
    source_path: Optional[Path],
    raw_offset: Any,
    *,
    cache: Dict[Path, bytes],
) -> Tuple[Optional[int], Optional[int]]:
    """Convert one Clang byte offset to tree-sitter line/byte-column."""
    if (
        source_path is None
        or isinstance(raw_offset, bool)
        or not isinstance(raw_offset, int)
        or raw_offset < 0
    ):
        return None, None
    path = source_path.resolve()
    try:
        source = cache.get(path)
        if source is None:
            source = path.read_bytes()
            cache[path] = source
    except OSError:
        return None, None
    if raw_offset > len(source):
        return None, None
    line = source.count(b"\n", 0, raw_offset) + 1
    previous_newline = source.rfind(b"\n", 0, raw_offset)
    col0 = raw_offset if previous_newline < 0 else raw_offset - previous_newline - 1
    return line, col0


def _declaration_start_line_col(
    loc: Optional[Dict[str, Any]],
    *,
    last_file: Optional[str],
    range_begin: Optional[Dict[str, Any]],
    resolved_start_path: Optional[Path],
    source_cache: Dict[Path, bytes],
) -> Tuple[Optional[int], Optional[int], Optional[int], str, Optional[str]]:
    """Return (line, col0, clang_col1, origin, file) for declaration *start*.

    Prefer ``range.begin`` (declaration start, aligned with tree-sitter node
    starts) over ``loc`` (often the name token). Macro disagreement is handled
    by the caller via resolve_function_location.
    """
    primary, _sp, _ex = resolve_function_location(
        loc, last_file=last_file, range_begin=range_begin
    )
    line: Optional[int] = None
    col1: Optional[int] = None
    origin = primary.origin
    file_val = primary.file

    if isinstance(range_begin, dict):
        if range_begin.get("line") is not None:
            line = _positive_ast_int(range_begin["line"])
        if range_begin.get("col") is not None:
            col1 = _positive_ast_int(range_begin["col"])
        if range_begin.get("file"):
            file_val = str(range_begin["file"])
            origin = "direct"

    if line is None and primary.line is not None:
        line = _positive_ast_int(primary.line)

    offset_line, offset_col0 = _source_line_col_from_offset(
        resolved_start_path,
        range_begin.get("offset") if isinstance(range_begin, dict) else None,
        cache=source_cache,
    )
    if offset_line is not None and offset_col0 is not None:
        # Offset is the strongest exact start coordinate. Direct line/column,
        # when present, must agree rather than being silently preferred.
        if line is not None and line != offset_line:
            return None, None, col1, origin, file_val
        if col1 is not None and col1 - 1 != offset_col0:
            return None, None, col1, origin, file_val
        line = offset_line
        col1 = offset_col0 + 1

    # ``loc.col`` is the declaration name token, not the declaration start.
    # Without range.begin column or offset we cannot claim exact identity.

    return line, _clang_col_to_zero_based(col1), col1, origin, file_val


def _parse_span_start(span: str) -> Tuple[Optional[int], Optional[int]]:
    m = _SPAN_RE.match(str(span or ""))
    if not m:
        return None, None
    return int(m.group("sl")), int(m.group("sc"))


def _has_enum_body(node: Dict[str, Any]) -> bool:
    for child in node.get("inner") or []:
        if isinstance(child, dict) and child.get("kind") == "EnumConstantDecl":
            return True
    return False


def _type_props(node: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    t = node.get("type") if isinstance(node.get("type"), dict) else {}
    qual = t.get("qualType")
    desug = t.get("desugaredQualType")
    return (
        str(qual) if qual is not None else None,
        str(desug) if desug is not None else None,
    )


def _fixed_underlying(node: Dict[str, Any]) -> Optional[str]:
    raw = node.get("fixedUnderlyingType")
    if isinstance(raw, dict):
        q = raw.get("qualType")
        return str(q) if q is not None else None
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


# ---------------------------------------------------------------------------
# AST collection
# ---------------------------------------------------------------------------


def collect_type_declarations_from_ast(
    root: Any,
    *,
    package_dir: Path,
    cwd: Path,
    entry_index: int,
    compiler_path: str,
    compiler_id: Optional[str],
    compile_commands_digest: str,
) -> Tuple[List[ClangTypeDeclaration], Set[str]]:
    """Walk one AST JSON root; collect type-declaration observations.

    Returns (all observations that are classified for reporting, in_scope_paths
    for package-local TU/declaration sites).
    """
    package_dir = package_dir.resolve()
    cwd = cwd.resolve()
    out: List[ClangTypeDeclaration] = []
    in_scope: Set[str] = set()
    source_cache: Dict[Path, bytes] = {}

    def emit(decl: ClangTypeDeclaration) -> None:
        out.append(decl)
        if decl.is_package_local and decl.source_path:
            in_scope.add(decl.source_path)

    def walk(node: Any, last_file: Optional[str]) -> Optional[str]:
        if not isinstance(node, dict):
            return last_file

        # Implicit builtins: do not update file context or collect.
        if node.get("isImplicit"):
            return last_file

        loc = node.get("loc") if isinstance(node.get("loc"), dict) else None
        if isinstance(loc, dict) and loc.get("file"):
            last_file = str(loc["file"])
        elif isinstance(loc, dict):
            for key in ("expansionLoc", "spellingLoc"):
                nested = loc.get(key)
                if isinstance(nested, dict) and nested.get("file"):
                    last_file = str(nested["file"])
                    break

        rng = node.get("range") if isinstance(node.get("range"), dict) else None
        range_begin = rng.get("begin") if isinstance(rng, dict) else None
        kind = node.get("kind")

        if kind in {"RecordDecl", "EnumDecl", "TypedefDecl"}:
            primary, spelling, expansion = resolve_function_location(
                loc, last_file=last_file, range_begin=range_begin
            )
            # Keep file context advancing on explicit files.
            if primary.file:
                last_file = primary.file

            spell_rel, spell_abs, spell_local = _normalize_path_token(
                spelling.file, cwd=cwd, package_dir=package_dir
            )
            exp_rel, exp_abs, exp_local = _normalize_path_token(
                expansion.file, cwd=cwd, package_dir=package_dir
            )
            start_file = (
                str(range_begin.get("file"))
                if isinstance(range_begin, dict) and range_begin.get("file")
                else primary.file
            )
            file_for_norm = start_file or primary.file
            prim_rel, prim_abs, prim_local = _normalize_path_token(
                file_for_norm, cwd=cwd, package_dir=package_dir
            )
            spell_path = spell_rel or (
                spell_abs.as_posix() if spell_abs is not None else None
            )
            expansion_path = exp_rel or (
                exp_abs.as_posix() if exp_abs is not None else None
            )
            primary_path = prim_rel or (
                prim_abs.as_posix() if prim_abs is not None else None
            )
            line, col0, col1, origin, _ = _declaration_start_line_col(
                loc,
                last_file=last_file,
                range_begin=range_begin,
                resolved_start_path=prim_abs,
                source_cache=source_cache,
            )

            name_raw = node.get("name")
            name = str(name_raw).strip() if name_raw else None
            if name == "":
                name = None

            entity_kind = (
                "struct"
                if kind == "RecordDecl"
                else "enum"
                if kind == "EnumDecl"
                else "typedef"
            )
            tag_kind = (
                str(node.get("tagUsed") or "") or None
                if kind == "RecordDecl"
                else None
            )
            qual_type, desugared_qual_type = (
                _type_props(node) if kind == "TypedefDecl" else (None, None)
            )
            declaration_properties = {
                "is_anonymous": name is None,
                "is_union": kind == "RecordDecl" and tag_kind == "union",
                "is_complete": (
                    bool(node.get("completeDefinition"))
                    if kind == "RecordDecl"
                    else _has_enum_body(node)
                    if kind == "EnumDecl"
                    else None
                ),
                "tag_kind": tag_kind,
                "qual_type": qual_type,
                "desugared_qual_type": desugared_qual_type,
                "fixed_underlying_type": (
                    _fixed_underlying(node) if kind == "EnumDecl" else None
                ),
            }

            base_kwargs = dict(
                line=line,
                col0=col0,
                clang_col1=col1,
                location_origin=origin,
                spelling_path=spell_path,
                expansion_path=expansion_path,
                entry_indices=[entry_index],
                compiler_path=compiler_path,
                compiler_id=compiler_id,
                compile_commands_digest=compile_commands_digest,
            )

            # Macro spelling/expansion multi-file disagreement.
            if (
                spelling.origin == "spelling"
                and expansion.origin == "expansion"
                and spell_local
                and exp_local
                and spell_rel
                and exp_rel
                and spell_rel != exp_rel
            ):
                emit(
                    ClangTypeDeclaration(
                        entity_kind=entity_kind,
                        name=name,
                        source_path=spell_rel,
                        is_package_local=True,
                        classification_hint="macro_location_unsupported",
                        **declaration_properties,
                        **base_kwargs,
                    )
                )
                for child in node.get("inner") or []:
                    last_file = walk(child, last_file)
                return last_file

            # Outside package / system.
            if not prim_local or not prim_rel:
                emit(
                    ClangTypeDeclaration(
                        entity_kind=entity_kind,
                        name=name,
                        source_path=primary_path,
                        is_package_local=False,
                        classification_hint="outside_package",
                        **declaration_properties,
                        **base_kwargs,
                    )
                )
                for child in node.get("inner") or []:
                    last_file = walk(child, last_file)
                return last_file

            # --- Package-local classifications ---
            if kind == "RecordDecl":
                tag = str(node.get("tagUsed") or "")
                complete = bool(node.get("completeDefinition"))
                if tag == "union":
                    emit(
                        ClangTypeDeclaration(
                            entity_kind="struct",  # not matched; residual
                            name=name,
                            source_path=prim_rel,
                            is_package_local=True,
                            is_anonymous=name is None,
                            is_union=True,
                            is_complete=complete,
                            tag_kind="union",
                            classification_hint=(
                                "anonymous" if name is None else "unsupported"
                            ),
                            **base_kwargs,
                        )
                    )
                elif name is None:
                    emit(
                        ClangTypeDeclaration(
                            entity_kind="struct",
                            name=None,
                            source_path=prim_rel,
                            is_package_local=True,
                            is_anonymous=True,
                            is_complete=complete,
                            tag_kind=tag or "struct",
                            classification_hint="anonymous",
                            **base_kwargs,
                        )
                    )
                elif not complete:
                    emit(
                        ClangTypeDeclaration(
                            entity_kind="struct",
                            name=name,
                            source_path=prim_rel,
                            is_package_local=True,
                            is_complete=False,
                            tag_kind=tag or "struct",
                            classification_hint="unsupported",
                            **base_kwargs,
                        )
                    )
                elif tag and tag != "struct":
                    emit(
                        ClangTypeDeclaration(
                            entity_kind="struct",
                            name=name,
                            source_path=prim_rel,
                            is_package_local=True,
                            is_complete=complete,
                            tag_kind=tag,
                            classification_hint="unsupported",
                            **base_kwargs,
                        )
                    )
                else:
                    # Named complete struct — matchable.
                    emit(
                        ClangTypeDeclaration(
                            entity_kind="struct",
                            name=name,
                            source_path=prim_rel,
                            is_package_local=True,
                            is_complete=True,
                            tag_kind="struct",
                            **base_kwargs,
                        )
                    )

            elif kind == "EnumDecl":
                complete = _has_enum_body(node)
                fixed = _fixed_underlying(node)
                if name is None:
                    emit(
                        ClangTypeDeclaration(
                            entity_kind="enum",
                            name=None,
                            source_path=prim_rel,
                            is_package_local=True,
                            is_anonymous=True,
                            is_complete=complete,
                            fixed_underlying_type=fixed,
                            classification_hint="anonymous",
                            **base_kwargs,
                        )
                    )
                elif not complete:
                    emit(
                        ClangTypeDeclaration(
                            entity_kind="enum",
                            name=name,
                            source_path=prim_rel,
                            is_package_local=True,
                            is_complete=False,
                            fixed_underlying_type=fixed,
                            classification_hint="unsupported",
                            **base_kwargs,
                        )
                    )
                else:
                    emit(
                        ClangTypeDeclaration(
                            entity_kind="enum",
                            name=name,
                            source_path=prim_rel,
                            is_package_local=True,
                            is_complete=True,
                            fixed_underlying_type=fixed,
                            **base_kwargs,
                        )
                    )

            elif kind == "TypedefDecl":
                if name is None:
                    emit(
                        ClangTypeDeclaration(
                            entity_kind="typedef",
                            name=None,
                            source_path=prim_rel,
                            is_package_local=True,
                            is_anonymous=True,
                            classification_hint="anonymous",
                            **base_kwargs,
                        )
                    )
                else:
                    qual, desug = _type_props(node)
                    emit(
                        ClangTypeDeclaration(
                            entity_kind="typedef",
                            name=name,
                            source_path=prim_rel,
                            is_package_local=True,
                            qual_type=qual,
                            desugared_qual_type=desug,
                            **base_kwargs,
                        )
                    )

        for child in node.get("inner") or []:
            last_file = walk(child, last_file)
        return last_file

    walk(root, None)
    return out, in_scope


# ---------------------------------------------------------------------------
# Tree-sitter side
# ---------------------------------------------------------------------------


def collect_tree_sitter_types(package_dir: Path) -> List[TreeSitterTypeEntity]:
    """All tree-sitter type declaration sites (multi-site per semantic entity).

    One parse per source file via :func:`collect_type_declaration_sites`.
    Does not call ``build_c_byog`` (avoids a second package parse).
    """
    package_dir = package_dir.resolve()
    try:
        sites: Sequence[TypeDeclarationSite] = collect_type_declaration_sites(
            package_dir
        )
    except (OSError, ValueError) as err:
        raise ClangTypeAuditError(
            f"failed to collect tree-sitter type declaration sites: {err}"
        ) from err

    out: List[TreeSitterTypeEntity] = []
    for site in sites:
        out.append(
            TreeSitterTypeEntity(
                title=site.title,
                entity_kind=site.entity_kind,
                name=site.name,
                source_path=site.source_path,
                line=site.line,
                col0=site.col0,
                span=site.span,
                is_canonical=site.is_canonical,
            )
        )
    return out


def _ts_config_evidence(ts: TreeSitterTypeEntity) -> Dict[str, Any]:
    dead = any(
        str(b).find('"liveness": "dead"') >= 0
        or (isinstance(b, str) and "branch_dead" in b)
        for b in ts.preprocessor_branches
    )
    dead = dead or any("branch_dead" in r for r in ts.preprocessor_reasons)
    unknown = any("branch_unknown" in r for r in ts.preprocessor_reasons) or any(
        '"liveness": "unknown"' in str(b) for b in ts.preprocessor_branches
    )
    return {
        "preprocessor_dependent": ts.preprocessor_dependent,
        "preprocessor_reasons": sorted(ts.preprocessor_reasons),
        "branch_dead_evidence": dead,
        "branch_unknown_evidence": unknown,
    }


def _site_record(ts: TreeSitterTypeEntity) -> Dict[str, Any]:
    return {
        "title": ts.title,
        "entity_kind": ts.entity_kind,
        "name": ts.name,
        "source_path": ts.source_path,
        "line": ts.line,
        "col0": ts.col0,
        "span": ts.span,
        "is_canonical": ts.is_canonical,
    }


# ---------------------------------------------------------------------------
# Merge + match
# ---------------------------------------------------------------------------


def _semantic_record(d: ClangTypeDeclaration) -> Dict[str, Any]:
    return {
        "entity_kind": d.entity_kind,
        "name": d.name,
        "source_path": d.source_path,
        "line": d.line,
        "col0": d.col0,
        "clang_col1": d.clang_col1,
        "location_origin": d.location_origin,
        "spelling_path": d.spelling_path,
        "expansion_path": d.expansion_path,
        "is_anonymous": d.is_anonymous,
        "is_union": d.is_union,
        "is_complete": d.is_complete,
        "tag_kind": d.tag_kind,
        "qualType": d.qual_type,
        "desugaredQualType": d.desugared_qual_type,
        "fixedUnderlyingType": d.fixed_underlying_type,
        "classification_hint": d.classification_hint,
    }


def merge_clang_type_declarations(
    decls: Sequence[ClangTypeDeclaration],
) -> List[ClangTypeDeclaration]:
    """Merge multi-entry observations; conflict → classification_hint."""

    groups: Dict[Tuple[Any, ...], List[ClangTypeDeclaration]] = {}
    for d in decls:
        groups.setdefault(d.merge_key(), []).append(d)

    merged: List[ClangTypeDeclaration] = []
    for _key, rows in groups.items():
        semantic_keys = {
            json.dumps(_semantic_record(d), sort_keys=True, separators=(",", ":"))
            for d in rows
        }
        observations: Dict[str, Dict[str, Any]] = {}
        for d in rows:
            rec = {
                **_semantic_record(d),
                "compiler_path": d.compiler_path,
                "compiler_id": d.compiler_id,
                "compile_commands_digest": d.compile_commands_digest,
            }
            obs_key = json.dumps(rec, sort_keys=True, separators=(",", ":"))
            if obs_key not in observations:
                observations[obs_key] = {
                    **rec,
                    "entry_indices": sorted(set(d.entry_indices)),
                }
            else:
                observations[obs_key]["entry_indices"] = sorted(
                    set(observations[obs_key]["entry_indices"])
                    | set(d.entry_indices)
                )
        chosen = min(
            rows,
            key=lambda d: json.dumps(
                {
                    **_semantic_record(d),
                    "compiler_path": d.compiler_path,
                    "compiler_id": d.compiler_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        hint = chosen.classification_hint
        if len(semantic_keys) > 1:
            # Same merge_key but property disagreement — treat as ambiguous.
            hint = "conflicting_compile_observations"
        merged.append(
            ClangTypeDeclaration(
                entity_kind=chosen.entity_kind,
                name=chosen.name,
                source_path=chosen.source_path,
                line=chosen.line,
                col0=chosen.col0,
                clang_col1=chosen.clang_col1,
                location_origin=chosen.location_origin,
                spelling_path=chosen.spelling_path,
                expansion_path=chosen.expansion_path,
                is_package_local=chosen.is_package_local,
                is_anonymous=chosen.is_anonymous,
                is_union=chosen.is_union,
                is_complete=chosen.is_complete,
                tag_kind=chosen.tag_kind,
                qual_type=chosen.qual_type,
                desugared_qual_type=chosen.desugared_qual_type,
                fixed_underlying_type=chosen.fixed_underlying_type,
                classification_hint=hint,
                entry_indices=sorted({i for d in rows for i in d.entry_indices}),
                compiler_path=chosen.compiler_path,
                compiler_id=chosen.compiler_id,
                compile_commands_digest=chosen.compile_commands_digest,
                observation_variants=[observations[k] for k in sorted(observations)],
            )
        )
    merged.sort(
        key=lambda d: (
            d.entity_kind,
            d.source_path or "",
            d.name or "",
            d.line or 0,
            d.col0 or 0,
            d.classification_hint or "",
        )
    )
    return merged


def _decl_row(d: ClangTypeDeclaration, **extra: Any) -> Dict[str, Any]:
    provenance = _declaration_provenance(d)
    row = {
        "entity_kind": d.entity_kind,
        "name": d.name,
        "source_path": d.source_path,
        "line": d.line,
        "col0": d.col0,
        "clang_col1": d.clang_col1,
        "location_origin": d.location_origin,
        "tag_kind": d.tag_kind,
        "is_complete": d.is_complete,
        "is_anonymous": d.is_anonymous,
        "is_union": d.is_union,
        "qualType": d.qual_type,
        "desugaredQualType": d.desugared_qual_type,
        "fixedUnderlyingType": d.fixed_underlying_type,
        **provenance,
    }
    row.update(extra)
    return row


def _declaration_provenance(d: ClangTypeDeclaration) -> Dict[str, Any]:
    """Canonical row-level summary without inventing one compiler identity."""
    compiler_map: Dict[Tuple[str, str, str], Dict[str, str]] = {}
    digests: Set[str] = set()
    for observation in d.observation_variants:
        compiler_path = observation.get("compiler_path")
        compiler_id = observation.get("compiler_id")
        digest = observation.get("compile_commands_digest")
        if (
            isinstance(compiler_path, str)
            and compiler_path
            and isinstance(compiler_id, str)
            and compiler_id
            and isinstance(digest, str)
            and digest
        ):
            key = (compiler_path, compiler_id, digest)
            compiler_map[key] = {
                "compiler_path": compiler_path,
                "compiler_id": compiler_id,
                "compile_commands_digest": digest,
            }
            digests.add(digest)
    compilers = [compiler_map[key] for key in sorted(compiler_map)]
    one = compilers[0] if len(compilers) == 1 else {}
    return {
        "entry_indices": list(d.entry_indices),
        "compiler_path": one.get("compiler_path"),
        "compiler_id": one.get("compiler_id"),
        "compile_commands_digest": (
            next(iter(digests)) if len(digests) == 1 else None
        ),
        "compilers": compilers,
        "observations": d.observation_variants,
    }


def match_type_declarations(
    *,
    clang_decls: Sequence[ClangTypeDeclaration],
    tree_sitter: Sequence[TreeSitterTypeEntity],
    in_scope_paths: Set[str],
) -> Dict[str, Any]:
    """Classify type declarations into the required audit buckets.

    Tree-sitter input is multi-site: several sites may share one semantic
    graph title. Matching is exact site identity only; alternate sites of a
    matched entity are reported as non-failing diagnostics.
    """
    merged = merge_clang_type_declarations(clang_decls)

    matched: List[Dict[str, Any]] = []
    tree_sitter_only: List[Dict[str, Any]] = []
    out_of_scope: List[Dict[str, Any]] = []
    clang_only: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    macro_unsupported: List[Dict[str, Any]] = []
    anonymous: List[Dict[str, Any]] = []
    unsupported: List[Dict[str, Any]] = []
    outside: List[Dict[str, Any]] = []
    alternate_sites: List[Dict[str, Any]] = []

    # Index sites by exact identity and by title (semantic graph entity).
    ts_by_id: Dict[Tuple[str, str, str, int, int], List[TreeSitterTypeEntity]] = {}
    ts_by_title: Dict[str, List[TreeSitterTypeEntity]] = {}
    for t in tree_sitter:
        ts_by_title.setdefault(t.title, []).append(t)
        ts_by_id.setdefault(t.identity(), []).append(t)

    claimed_titles: Set[str] = set()

    def _claim_title(title: str) -> None:
        claimed_titles.add(title)

    def _canonical_site(title: str) -> Optional[TreeSitterTypeEntity]:
        sites = ts_by_title.get(title) or []
        for site in sites:
            if site.is_canonical:
                return site
        return sites[0] if sites else None

    for d in merged:
        hint = d.classification_hint
        if hint == "outside_package":
            outside.append(
                _decl_row(d, classification="outside_package_declarations")
            )
            continue
        if hint == "anonymous":
            anonymous.append(
                _decl_row(d, classification="anonymous_declarations")
            )
            continue
        if hint == "unsupported":
            unsupported.append(
                _decl_row(d, classification="unsupported_declarations")
            )
            continue
        if hint == "macro_location_unsupported":
            macro_unsupported.append(
                _decl_row(
                    d,
                    reason=(
                        "spelling and expansion resolve to different package files"
                    ),
                    classification="macro_location_unsupported",
                )
            )
            continue
        if hint == "conflicting_compile_observations":
            ident = d.matchable_identity()
            exact = ts_by_id.get(ident) or [] if ident is not None else []
            titles = sorted({t.title for t in exact})
            for title in titles:
                _claim_title(title)
            ambiguous.append(
                _decl_row(
                    d,
                    reason="compile entries produced conflicting Clang observations",
                    tree_sitter_candidates=[
                        _site_record(t)
                        for t in sorted(
                            exact,
                            key=lambda item: (
                                item.line,
                                item.col0,
                                item.title,
                                item.span,
                            ),
                        )
                    ],
                    classification="ambiguous",
                )
            )
            continue

        ident = d.matchable_identity()
        if ident is None:
            unsupported.append(
                _decl_row(
                    d,
                    classification="unsupported_declarations",
                    reason="missing exact line/column for matchable declaration",
                )
            )
            continue

        exact = ts_by_id.get(ident) or []

        if not exact:
            # No exact site. Do not fall back to coordinate-free matching.
            # Collect same kind+path+name sites only for residual diagnostics.
            same_name_sites = [
                t
                for t in tree_sitter
                if t.entity_kind == ident[0]
                and t.source_path == ident[1]
                and t.name == ident[2]
                and t.title not in claimed_titles
            ]
            if same_name_sites:
                for t in same_name_sites:
                    _claim_title(t.title)
                ambiguous.append(
                    _decl_row(
                        d,
                        reason=(
                            "kind+path+name agree but exact line/column does not "
                            "match any tree-sitter declaration site "
                            "(wrong line/column cannot match)"
                        ),
                        tree_sitter_candidates=[
                            _site_record(t)
                            for t in sorted(
                                same_name_sites,
                                key=lambda x: (x.line, x.col0, x.title, x.span),
                            )
                        ],
                        classification="ambiguous",
                    )
                )
            else:
                clang_only.append(_decl_row(d, classification="clang_only"))
            continue

        # Exact sites must all belong to one semantic title.
        titles = {t.title for t in exact}
        if len(titles) > 1:
            for title in titles:
                _claim_title(title)
            ambiguous.append(
                _decl_row(
                    d,
                    reason=(
                        "exact declaration site maps to multiple semantic "
                        "tree-sitter entities"
                    ),
                    tree_sitter_titles=sorted(titles),
                    classification="ambiguous",
                )
            )
            continue

        if len(exact) > 1:
            # Same title, same exact coordinates twice — fail closed.
            for title in titles:
                _claim_title(title)
            ambiguous.append(
                _decl_row(
                    d,
                    reason=(
                        "multiple tree-sitter declaration sites share the exact "
                        "type identity"
                    ),
                    tree_sitter_titles=sorted(titles),
                    classification="ambiguous",
                )
            )
            continue

        matched_site = exact[0]
        title = matched_site.title
        if title in claimed_titles:
            # Another clang row already claimed this semantic entity.
            ambiguous.append(
                _decl_row(
                    d,
                    reason=(
                        "two configured Clang declarations claim the same "
                        "semantic tree-sitter entity"
                    ),
                    tree_sitter_title=title,
                    classification="ambiguous",
                )
            )
            continue

        _claim_title(title)
        all_sites = sorted(
            ts_by_title.get(title) or [],
            key=lambda s: (s.line, s.col0, s.span, not s.is_canonical),
        )
        canonical = _canonical_site(title) or matched_site
        provenance = _declaration_provenance(d)
        matched.append(
            {
                "entity_kind": d.entity_kind,
                "name": d.name,
                "source_path": d.source_path,
                "tree_sitter_title": title,
                # Canonical graph representative (unchanged by configuration).
                "graph_canonical_span": canonical.span,
                "graph_canonical_line": canonical.line,
                "graph_canonical_col0": canonical.col0,
                "graph_canonical_is_matched_site": (
                    canonical.line == matched_site.line
                    and canonical.col0 == matched_site.col0
                    and canonical.span == matched_site.span
                ),
                # Exact configured match site (may differ from canonical).
                "matched_site_span": matched_site.span,
                "matched_site_line": matched_site.line,
                "matched_site_col0": matched_site.col0,
                "matched_site_is_canonical": matched_site.is_canonical,
                # Back-compat fields: report the exact matched site coordinates.
                "tree_sitter_line": matched_site.line,
                "tree_sitter_col": matched_site.col0,
                "clang_line": d.line,
                "clang_col0": d.col0,
                "clang_col1": d.clang_col1,
                "line_column_confirmed": True,
                "tag_kind": d.tag_kind,
                "is_complete": d.is_complete,
                "qualType": d.qual_type,
                "desugaredQualType": d.desugared_qual_type,
                "fixedUnderlyingType": d.fixed_underlying_type,
                "location_origin": d.location_origin,
                **provenance,
            }
        )
        for site in all_sites:
            if (
                site.line == matched_site.line
                and site.col0 == matched_site.col0
                and site.span == matched_site.span
            ):
                continue
            alternate_sites.append(
                {
                    "classification": "alternate_declaration_sites",
                    "entity_kind": site.entity_kind,
                    "name": site.name,
                    "source_path": site.source_path,
                    "tree_sitter_title": site.title,
                    "line": site.line,
                    "col0": site.col0,
                    "span": site.span,
                    "is_canonical": site.is_canonical,
                    "matched_site_line": matched_site.line,
                    "matched_site_col0": matched_site.col0,
                    "matched_site_span": matched_site.span,
                    "note": (
                        "exact declaration site owned by a matched semantic "
                        "entity but not selected by the current Clang "
                        "configuration; not proven dead or inactive"
                    ),
                }
            )

    # Unmatched semantic entities (by title), entity-level residuals.
    for title, sites in sorted(ts_by_title.items()):
        if title in claimed_titles:
            continue
        # Prefer canonical site for residual location reporting.
        canonical = next((s for s in sites if s.is_canonical), sites[0])
        base = {
            "entity_kind": canonical.entity_kind,
            "name": canonical.name,
            "source_path": canonical.source_path,
            "tree_sitter_title": title,
            "line": canonical.line,
            "col0": canonical.col0,
            "span": canonical.span,
            "declaration_site_count": len(sites),
            **_ts_config_evidence(canonical),
        }
        if canonical.source_path not in in_scope_paths:
            out_of_scope.append(
                {**base, "classification": "out_of_compile_db_scope"}
            )
        else:
            tree_sitter_only.append(
                {**base, "classification": "tree_sitter_only"}
            )

    def sort_recs(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            rows,
            key=lambda r: (
                str(r.get("entity_kind") or ""),
                str(r.get("source_path") or ""),
                str(r.get("name") or ""),
                str(r.get("tree_sitter_title") or ""),
                int(
                    r.get("line")
                    or r.get("clang_line")
                    or r.get("matched_site_line")
                    or r.get("tree_sitter_line")
                    or 0
                ),
                int(
                    r.get("col0")
                    or r.get("clang_col0")
                    or r.get("matched_site_col0")
                    or r.get("tree_sitter_col")
                    or 0
                ),
                json.dumps(r, sort_keys=True),
            ),
        )

    buckets = {
        "matched": sort_recs(matched),
        "tree_sitter_only": sort_recs(tree_sitter_only),
        "out_of_compile_db_scope": sort_recs(out_of_scope),
        "clang_only": sort_recs(clang_only),
        "ambiguous": sort_recs(ambiguous),
        "macro_location_unsupported": sort_recs(macro_unsupported),
        "anonymous_declarations": sort_recs(anonymous),
        "unsupported_declarations": sort_recs(unsupported),
        "outside_package_declarations": sort_recs(outside),
        "alternate_declaration_sites": sort_recs(alternate_sites),
    }
    counts = {k: len(buckets[k]) for k in _ALL_BUCKETS}
    counts["clang_type_declarations_package_local"] = sum(
        1 for d in merged if d.is_package_local
    )
    # Semantic entities (unique titles), not raw site count.
    semantic_titles = set(ts_by_title)
    counts["tree_sitter_type_entities_total"] = len(semantic_titles)
    counts["tree_sitter_type_entities_in_scope"] = sum(
        1
        for title, sites in ts_by_title.items()
        if any(s.source_path in in_scope_paths for s in sites)
    )
    counts["tree_sitter_declaration_sites_total"] = len(tree_sitter)
    return {"buckets": buckets, "counts": counts}


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------


def _normalize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    text = json.dumps(report, sort_keys=True, separators=(",", ":"))
    return json.loads(text)


def audit_to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, indent=2) + "\n"


def _validated_entry_indices(
    value: Any,
    *,
    n_compile_entries: int,
    context: str,
) -> List[int]:
    if not isinstance(value, list) or not value:
        raise ClangTypeAuditError(f"{context} entry_indices must be non-empty")
    for index in value:
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or index < 0
            or index >= n_compile_entries
        ):
            raise ClangTypeAuditError(
                f"{context} has invalid compile entry index {index!r}"
            )
    if value != sorted(set(value)):
        raise ClangTypeAuditError(
            f"{context} entry_indices must be sorted and unique"
        )
    return list(value)


def _validate_report_observation_provenance(
    report: Dict[str, Any],
    capture: Any,
) -> None:
    """Bind every Clang-derived row/observation to the capture entry census."""
    entry_compilers = {
        entry.entry_index: (entry.compiler_path, entry.compiler_id)
        for entry in capture.entries
    }
    derived_buckets = (
        "matched",
        "clang_only",
        "ambiguous",
        "macro_location_unsupported",
        "anonymous_declarations",
        "unsupported_declarations",
        "outside_package_declarations",
    )
    for bucket in derived_buckets:
        rows = report.get(bucket)
        if not isinstance(rows, list):
            raise ClangTypeAuditError(f"report bucket {bucket!r} is not a list")
        for position, row in enumerate(rows):
            context = f"{bucket}[{position}]"
            if not isinstance(row, dict):
                raise ClangTypeAuditError(f"{context} is not an object")
            row_indices = _validated_entry_indices(
                row.get("entry_indices"),
                n_compile_entries=capture.n_compile_entries,
                context=context,
            )
            if (
                row.get("compile_commands_digest")
                != capture.compile_commands_digest
            ):
                raise ClangTypeAuditError(
                    f"{context} compile_commands_digest disagrees with capture"
                )
            observations = row.get("observations")
            if not isinstance(observations, list) or not observations:
                raise ClangTypeAuditError(
                    f"{context} observations must be a non-empty list"
                )
            observed_indices: Set[int] = set()
            observed_compilers: Dict[Tuple[str, str, str], Dict[str, str]] = {}
            for obs_position, observation in enumerate(observations):
                obs_context = f"{context}.observations[{obs_position}]"
                if not isinstance(observation, dict):
                    raise ClangTypeAuditError(f"{obs_context} is not an object")
                indices = _validated_entry_indices(
                    observation.get("entry_indices"),
                    n_compile_entries=capture.n_compile_entries,
                    context=obs_context,
                )
                compiler_path = observation.get("compiler_path")
                compiler_id = observation.get("compiler_id")
                digest = observation.get("compile_commands_digest")
                if (
                    not isinstance(compiler_path, str)
                    or not compiler_path
                    or not isinstance(compiler_id, str)
                    or not compiler_id
                    or digest != capture.compile_commands_digest
                ):
                    raise ClangTypeAuditError(
                        f"{obs_context} has incomplete compiler/digest provenance"
                    )
                for index in indices:
                    if entry_compilers[index] != (compiler_path, compiler_id):
                        raise ClangTypeAuditError(
                            f"{obs_context} compiler disagrees with capture "
                            f"entry {index}"
                        )
                observed_indices.update(indices)
                key = (compiler_path, compiler_id, digest)
                observed_compilers[key] = {
                    "compiler_path": compiler_path,
                    "compiler_id": compiler_id,
                    "compile_commands_digest": digest,
                }
            if observed_indices != set(row_indices):
                raise ClangTypeAuditError(
                    f"{context} entry_indices disagree with observations"
                )
            expected_compilers = [
                observed_compilers[key] for key in sorted(observed_compilers)
            ]
            if row.get("compilers") != expected_compilers:
                raise ClangTypeAuditError(
                    f"{context} compilers disagree with observations"
                )
            if len(expected_compilers) == 1:
                one = expected_compilers[0]
                if (
                    row.get("compiler_path") != one["compiler_path"]
                    or row.get("compiler_id") != one["compiler_id"]
                ):
                    raise ClangTypeAuditError(
                        f"{context} singular compiler disagrees with observations"
                    )
            elif (
                row.get("compiler_path") is not None
                or row.get("compiler_id") is not None
            ):
                raise ClangTypeAuditError(
                    f"{context} multi-compiler row exposes a singular compiler"
                )


def build_type_declaration_audit_from_capture(capture: Any) -> Dict[str, Any]:
    """Build the type-declaration audit from an in-memory capture.

    Never invokes the compiler or reloads ``compile_commands.json``.
    """
    from c_clang_ast_capture import (  # type: ignore
        ClangAstCaptureError,
        ClangAstPackageCapture,
        assert_audit_report_matches_capture,
        validate_clang_ast_capture,
    )

    if not isinstance(capture, ClangAstPackageCapture):
        raise ClangTypeAuditError(
            "build_type_declaration_audit_from_capture requires a "
            "ClangAstPackageCapture"
        )
    try:
        validate_clang_ast_capture(capture)
    except ClangAstCaptureError as e:
        raise ClangTypeAuditError(str(e)) from e

    package_dir = capture.package_dir
    digest = capture.compile_commands_digest
    all_decls: List[ClangTypeDeclaration] = []
    translation_units: List[Dict[str, Any]] = []
    in_scope_paths: Set[str] = set()

    for ent in capture.entries:
        tu_path = ent.tu_path
        if path_is_under(tu_path, package_dir):
            in_scope_paths.add(package_relative_posix(tu_path, package_dir))

        decls, scope = collect_type_declarations_from_ast(
            ent.ast_root,
            package_dir=package_dir,
            cwd=ent.cwd,
            entry_index=ent.entry_index,
            compiler_path=ent.compiler_path,
            compiler_id=ent.compiler_id,
            compile_commands_digest=digest,
        )
        in_scope_paths |= scope
        all_decls.extend(decls)
        tu_local = path_is_under(tu_path, package_dir)
        translation_units.append(
            {
                "entry_index": ent.entry_index,
                "file": package_relative_posix(tu_path, package_dir)
                if tu_local
                else None,
                "package_local": tu_local,
                "compiler_path": ent.compiler_path,
                "compiler_id": ent.compiler_id,
                "n_type_declarations_observed": len(decls),
            }
        )

    ts_types = collect_tree_sitter_types(package_dir)
    compared = match_type_declarations(
        clang_decls=all_decls,
        tree_sitter=ts_types,
        in_scope_paths=in_scope_paths,
    )
    buckets = compared["buckets"]
    counts = compared["counts"]

    compiler_list = list(capture.compilers)
    one = compiler_list[0] if len(compiler_list) == 1 else {}

    report: Dict[str, Any] = {
        "mode": MODE,
        "package": package_dir.name,
        "compiler_path": one.get("compiler_path"),
        "compiler_id": one.get("compiler_id"),
        "compiler_version": one.get("compiler_version"),
        "compilers": compiler_list,
        "compile_commands_digest": digest,
        "n_compile_entries": capture.n_compile_entries,
        "translation_units": sorted(
            translation_units,
            key=lambda t: (t["entry_index"], str(t.get("file") or "")),
        ),
        "in_scope_source_paths": sorted(in_scope_paths),
        "column_convention": {
            "tree_sitter_entity_span": (
                "line:col-line:col with 1-based line and 0-based byte column "
                "(node start)"
            ),
            "clang_declaration_start": (
                "range.begin preferred; Clang 1-based col normalized to 0-based"
            ),
            "match_identity": (
                "entity_kind + package-relative path + name + exact line + exact col0"
            ),
            "never_match_by": "bare name or title alone; column-only; line tolerance",
        },
        "fail_on_mismatch_policy": {
            "exit_1_buckets": list(_FAIL_ON_MISMATCH_BUCKETS),
            "observation_only_buckets": list(_OBSERVATION_ONLY_BUCKETS),
            "note": (
                "out_of_compile_db_scope, anonymous_declarations, "
                "unsupported_declarations, outside_package_declarations, and "
                "alternate_declaration_sites do not by themselves cause "
                "--fail-on-mismatch to exit 1"
            ),
        },
        "counts": counts,
        "matched": buckets["matched"],
        "tree_sitter_only": buckets["tree_sitter_only"],
        "out_of_compile_db_scope": buckets["out_of_compile_db_scope"],
        "clang_only": buckets["clang_only"],
        "ambiguous": buckets["ambiguous"],
        "macro_location_unsupported": buckets["macro_location_unsupported"],
        "anonymous_declarations": buckets["anonymous_declarations"],
        "unsupported_declarations": buckets["unsupported_declarations"],
        "outside_package_declarations": buckets["outside_package_declarations"],
        "alternate_declaration_sites": buckets["alternate_declaration_sites"],
        "limitations": [
            "Standalone CLI is diagnostic only; optional --clang-types may attach matched fields (no uses_type edges)",
            "Named complete struct (RecordDecl tagUsed=struct completeDefinition) only",
            "Named complete enum (EnumDecl with EnumConstantDecl body) only",
            "Package-local TypedefDecl only",
            "Anonymous / union / incomplete / unsupported / outside-package are residuals",
            "Identity = entity_kind + path + name + exact line + exact col0",
            "Struct and typedef with the same title remain distinct identities",
            "Not layout/ABI, not type-use analysis, not points-to, not C++, not multi-config",
            "Macro spelling/expansion multi-file disagreement is not guessed",
            (
                "Graph keeps one canonical source-derived site per semantic "
                "entity; audit may match any exact owned site"
            ),
            (
                "alternate_declaration_sites are configuration-unselected "
                "owned sites, not proven dead/inactive"
            ),
        ],
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }

    # Enforce count/list agreement before normalize.
    for key in _ALL_BUCKETS:
        if counts.get(key) != len(report[key]):
            raise ClangTypeAuditError(
                f"internal count/list mismatch for {key}: "
                f"count={counts.get(key)} rows={len(report[key])}"
            )

    normalized = _normalize_report(report)
    _validate_report_observation_provenance(normalized, capture)
    try:
        assert_audit_report_matches_capture(
            normalized, capture, context="type-declaration audit"
        )
    except ClangAstCaptureError as e:
        raise ClangTypeAuditError(str(e)) from e
    return normalized


def run_clang_type_audit(
    package_dir: Path,
    *,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Run the type-declaration audit (one capture, then pure builder)."""
    from c_clang_ast_capture import (  # type: ignore
        ClangAstCaptureError,
        capture_clang_ast_package,
    )

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout <= 0
    ):
        raise ClangTypeAuditError(
            f"timeout must be a positive integer, got {timeout!r}"
        )
    try:
        capture = capture_clang_ast_package(package_dir, timeout=timeout)
    except ClangAstCaptureError as e:
        raise ClangTypeAuditError(str(e)) from e
    return build_type_declaration_audit_from_capture(capture)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Clang AST JSON type-declaration audit vs tree-sitter-c "
            "struct/enum/typedef entities (diagnostic only; no BYOG mutation)."
        )
    )
    parser.add_argument(
        "--package",
        "-p",
        type=Path,
        required=True,
        help="C package directory containing compile_commands.json",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Write JSON report to this path (default: stdout)",
    )
    parser.add_argument(
        "--fail-on-mismatch",
        action="store_true",
        help=(
            "Exit 1 when tree_sitter_only, clang_only, ambiguous, or "
            "macro_location_unsupported counts are non-zero. "
            "out_of_compile_db_scope, anonymous, unsupported, and "
            "outside-package observations alone do not fail."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Per-entry Clang AST dump timeout in seconds (default 120)",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.timeout <= 0:
        parser.error("--timeout must be a positive integer")

    try:
        report = run_clang_type_audit(args.package, timeout=args.timeout)
    except ClangTypeAuditError as e:
        print(f"c_clang_type_audit: {e}", file=sys.stderr)
        return 2
    except ClangAstAuditError as e:
        print(f"c_clang_type_audit: {e}", file=sys.stderr)
        return 2

    text = audit_to_json(report)
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        except OSError as e:
            print(
                f"c_clang_type_audit: failed to write output: {e}",
                file=sys.stderr,
            )
            return 2
    else:
        sys.stdout.write(text)

    if args.fail_on_mismatch:
        counts = report.get("counts") or {}
        residual = sum(int(counts.get(k) or 0) for k in _FAIL_ON_MISMATCH_BUCKETS)
        if residual:
            print(
                "c_clang_type_audit: --fail-on-mismatch: "
                + ", ".join(
                    f"{k}={counts.get(k)}"
                    for k in _FAIL_ON_MISMATCH_BUCKETS
                    if int(counts.get(k) or 0)
                ),
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
