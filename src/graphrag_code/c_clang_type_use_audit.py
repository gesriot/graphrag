#!/usr/bin/env python
"""Clang AST JSON type-use evidence audit (diagnostic only).

Inventories Clang-observed *type uses* on declaration-bearing AST nodes
(function returns, parameters, locals, fields, globals, typedef underlying
types) and classifies them against existing tree-sitter function/type entities.

This is **not** a graph overlay:
  * no ``uses_type`` relationships
  * no entity/relationship field publication
  * no ``index_c`` flag or snapshot manifest block

Location honesty: recorded coordinates are the **declaration-bearing node**
(loc / range.begin), not an exact type-token span. Clang AST JSON on probed
hosts does not supply a reliable type-token range for these uses.

Reuses ``build_function_audit_from_capture`` and
``build_type_declaration_audit_from_capture`` for owner/target identity —
does not reimplement their matching logic.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from graphrag_code.c_clang_ast_audit import (  # type: ignore
    ClangAstAuditError,
    _normalize_path_token,
    build_function_audit_from_capture,
    resolve_function_location,
)
from graphrag_code.c_clang_type_audit import (  # type: ignore
    ClangTypeAuditError,
    build_type_declaration_audit_from_capture,
)
from graphrag_code.c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    path_is_under,
)
from graphrag_code.c_identities import package_relative_posix  # type: ignore

MODE = "clang_ast_json_type_use_audit"
CONFIDENCE_BOUNDARY = (
    "Type-use classifications are configuration/toolchain-derived from the "
    "recorded Clang + compile_commands.json only. Locations are the "
    "declaration-bearing AST node (not proven exact type-token spans). "
    "This is not a uses_type graph, not layout/ABI proof, not multi-config "
    "semantics, not points-to analysis, and not C++. Residual buckets do not "
    "invent owner or target graph identities. No BYOG mutation."
)

# --fail-on-mismatch exit 1 when any of these are nonzero (internal candidates).
_FAIL_ON_MISMATCH_BUCKETS = (
    "owner_unmatched",
    "target_unresolved",
    "ambiguous_target",
    "macro_location_unsupported",
)

# Visible residuals that do not alone fail --fail-on-mismatch.
_OBSERVATION_ONLY_BUCKETS = (
    "external_or_system",
    "unsupported_type_form",
    "unowned_context",
)

_ALL_BUCKETS = (
    "matched_internal",
    *_FAIL_ON_MISMATCH_BUCKETS,
    *_OBSERVATION_ONLY_BUCKETS,
)

_USE_KINDS = (
    "function_return",
    "parameter",
    "local_variable",
    "field",
    "global_variable",
    "typedef_underlying",
)

_RESOLVERS = (
    "type_alias_decl_id",
    "exact_tag_spelling",
    "unique_typedef_spelling",
)

_OWNER_RESOLVERS = (
    "exact_declaration_site",
    "unique_external_function_name",
    "unique_internal_function_name_same_file",
    "owned_tag_typedef_site",
)

# Single-token builtins / libc primitives treated as external (not package types).
_BUILTIN_OR_LIBC = frozenset(
    {
        "void",
        "char",
        "short",
        "int",
        "long",
        "float",
        "double",
        "signed",
        "unsigned",
        "_Bool",
        "bool",
        "size_t",
        "ssize_t",
        "ptrdiff_t",
        "intptr_t",
        "uintptr_t",
        "FILE",
        "va_list",
        "fpos_t",
        "off_t",
        "time_t",
        "clock_t",
        "wchar_t",
        "wint_t",
        "max_align_t",
        "nullptr_t",
    }
)

_MULTIWORD_BUILTIN_RE = re.compile(
    r"^(?:"
    r"(?:unsigned|signed)\s+(?:char|short|int|long(?:\s+long)?)"
    r"|long\s+long(?:\s+int)?"
    r"|short\s+int"
    r"|long\s+int"
    r"|long\s+double"
    r"|unsigned|signed"
    r")$"
)

_TAG_RE = re.compile(r"^(struct|enum|union)\s+([A-Za-z_][\w]*)$")
_IDENT_RE = re.compile(r"^[A-Za-z_][\w]*$")
_INT_FIXED_RE = re.compile(r"^u?int(?:8|16|32|64)_t$")


class ClangTypeUseAuditError(ClangAstAuditError):
    """Raised when the type-use audit cannot run honestly."""


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _clang_col_to_zero_based(col1: Optional[int]) -> Optional[int]:
    if col1 is None or col1 < 1:
        return None
    return col1 - 1


def split_function_return_qual_type(qual_type: str) -> Optional[str]:
    """Extract return type from a FunctionProto ``qualType`` string.

    Finds the outermost parameter-list parentheses from the end. Does **not**
    claim this is a source token range — it is a string parse of Clang's
    type rendering.
    """
    s = str(qual_type or "").strip()
    if not s.endswith(")"):
        return None
    depth = 0
    for i in range(len(s) - 1, -1, -1):
        ch = s[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                ret = s[:i].strip()
                return ret or None
    return None


def is_function_pointer_qual_type(qual_type: str) -> bool:
    s = str(qual_type or "")
    return "(*)" in s or bool(re.search(r"\(\s*\*", s))


def strip_pointers_and_qualifiers(qual_type: str) -> str:
    """Peel pointer stars, arrays, and cv/restrict from a rendered type."""
    s = str(qual_type or "").strip()
    if not s:
        return ""
    # Drop array suffixes repeatedly.
    while True:
        nxt = re.sub(r"\s*\[[^\]]*\]\s*$", "", s).strip()
        if nxt == s:
            break
        s = nxt
    changed = True
    while changed:
        changed = False
        nxt = re.sub(r"^(?:const|volatile|restrict|_Atomic)\s+", "", s).strip()
        # Trailing cv may be spaced (``int const``) or glued to stars
        # (``cJSON *const`` / ``char * restrict``).
        nxt = re.sub(
            r"(?:\s+|\*+)(?:const|volatile|restrict)\s*$",
            lambda m: "*" * m.group(0).count("*"),
            nxt,
        ).strip()
        nxt = re.sub(r"\s+(?:const|volatile|restrict)\s*$", "", nxt).strip()
        nxt = re.sub(r"\s*\*+\s*$", "", nxt).strip()
        nxt = re.sub(r"^(?:const|volatile|restrict)\s+", "", nxt).strip()
        if nxt != s:
            s = nxt
            changed = True
    return s


def is_external_primitive(core: str) -> bool:
    if not core:
        return True
    if core in _BUILTIN_OR_LIBC:
        return True
    if _MULTIWORD_BUILTIN_RE.match(core):
        return True
    if _INT_FIXED_RE.match(core):
        return True
    if core.startswith("__"):
        return True
    return False


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RawTypeUse:
    """One Clang type-use observation before classification/dedup."""

    use_kind: str
    owner_kind: Optional[str]
    owner_name: Optional[str]
    owner_source_path: Optional[str]
    owner_line: Optional[int]
    owner_col0: Optional[int]
    owner_clang_id: Optional[str]
    target_qual_type: str
    target_desugared_qual_type: Optional[str]
    type_alias_decl_id: Optional[str]
    source_path: Optional[str]
    line: Optional[int]
    col0: Optional[int]
    byte_offset: Optional[int]
    location_origin: str
    location_precision: str
    is_package_local: bool
    classification_hint: Optional[str]
    entry_index: int
    compiler_path: str
    compiler_id: str
    compile_commands_digest: str
    # Enclosing context for owner resolution.
    enclosing_function_name: Optional[str] = None
    enclosing_function_path: Optional[str] = None
    enclosing_function_line: Optional[int] = None
    enclosing_record_name: Optional[str] = None
    enclosing_record_kind: Optional[str] = None  # struct|union
    enclosing_record_path: Optional[str] = None
    owner_storage_class: Optional[str] = None


@dataclass
class PackageTypeDecl:
    clang_id: str
    entity_kind: str  # struct|enum|typedef|union
    name: str
    source_path: str
    line: Optional[int]
    col0: Optional[int]


# ---------------------------------------------------------------------------
# AST collection
# ---------------------------------------------------------------------------


def _node_location(
    node: Dict[str, Any],
    *,
    last_file: Optional[str],
    cwd: Path,
    package_dir: Path,
) -> Tuple[
    Optional[str],
    Optional[int],
    Optional[int],
    Optional[int],
    str,
    bool,
    Optional[str],
]:
    """Return (rel_path, line, col0, byte_offset, origin, is_local, abs_hint).

    Coordinates are for the declaration-bearing node (prefer range.begin).
    """
    loc = node.get("loc") if isinstance(node.get("loc"), dict) else None
    rng = node.get("range") if isinstance(node.get("range"), dict) else None
    range_begin = rng.get("begin") if isinstance(rng, dict) else None
    primary, spelling, expansion = resolve_function_location(
        loc, last_file=last_file, range_begin=range_begin
    )

    # Macro multi-file disagreement within package → unsupported hint later.
    spell_rel, _, spell_local = _normalize_path_token(
        spelling.file, cwd=cwd, package_dir=package_dir
    )
    exp_rel, _, exp_local = _normalize_path_token(
        expansion.file, cwd=cwd, package_dir=package_dir
    )
    if (
        spelling.origin == "spelling"
        and expansion.origin == "expansion"
        and spell_local
        and exp_local
        and spell_rel
        and exp_rel
        and spell_rel != exp_rel
    ):
        return (
            spell_rel,
            _positive_int(spelling.line),
            _clang_col_to_zero_based(_positive_int(spelling.col)),
            None,
            "spelling+expansion",
            True,
            "macro_location_unsupported",
        )

    line: Optional[int] = None
    col1: Optional[int] = None
    origin = primary.origin
    file_val = primary.file
    byte_offset: Optional[int] = None

    if isinstance(range_begin, dict):
        if range_begin.get("line") is not None:
            line = _positive_int(range_begin.get("line"))
        if range_begin.get("col") is not None:
            col1 = _positive_int(range_begin.get("col"))
        if range_begin.get("file"):
            file_val = str(range_begin["file"])
            origin = "direct"
        off = range_begin.get("offset")
        if not isinstance(off, bool) and isinstance(off, int) and off >= 0:
            byte_offset = off

    if line is None and primary.line is not None:
        line = _positive_int(primary.line)
    if col1 is None and primary.col is not None:
        col1 = _positive_int(primary.col)

    rel, _, local = _normalize_path_token(
        file_val or last_file, cwd=cwd, package_dir=package_dir
    )
    return (
        rel if local else (rel or file_val),
        line,
        _clang_col_to_zero_based(col1),
        byte_offset,
        origin,
        bool(local and rel),
        None,
    )


def collect_package_type_decls_from_ast(
    root: Any,
    *,
    package_dir: Path,
    cwd: Path,
) -> Tuple[Dict[str, PackageTypeDecl], Dict[str, str]]:
    """Index package-local type decls by Clang id.

    Returns ``(decl_by_id, owned_record_to_typedef_name)`` where the second map
    links anonymous ``RecordDecl`` ids to the package-local typedef that owns
    them via ``ElaboratedType.ownedTagDecl`` (``typedef struct {…} Name``).
    """
    package_dir = package_dir.resolve()
    cwd = cwd.resolve()
    out: Dict[str, PackageTypeDecl] = {}
    owned_record_to_typedef: Dict[str, str] = {}

    def walk(node: Any, last_file: Optional[str]) -> Optional[str]:
        if not isinstance(node, dict):
            return last_file
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

        kind = node.get("kind")
        if (
            kind in {"TypedefDecl", "RecordDecl", "EnumDecl"}
            and node.get("id")
            and node.get("name")
        ):
            rel, line, col0, _bo, _origin, is_local, _hint = _node_location(
                node, last_file=last_file, cwd=cwd, package_dir=package_dir
            )
            if is_local and rel:
                if kind == "TypedefDecl":
                    entity_kind = "typedef"
                elif kind == "EnumDecl":
                    entity_kind = "enum"
                else:
                    tag = str(node.get("tagUsed") or "")
                    entity_kind = "union" if tag == "union" else "struct"
                out[str(node["id"])] = PackageTypeDecl(
                    clang_id=str(node["id"]),
                    entity_kind=entity_kind,
                    name=str(node["name"]),
                    source_path=rel,
                    line=line,
                    col0=col0,
                )
                # typedef struct {…} Name; → ownedTagDecl points at anon record.
                if kind == "TypedefDecl":
                    for child in node.get("inner") or []:
                        if not isinstance(child, dict):
                            continue
                        owned = child.get("ownedTagDecl")
                        if (
                            isinstance(owned, dict)
                            and owned.get("id")
                            and owned.get("kind") == "RecordDecl"
                        ):
                            owned_record_to_typedef[str(owned["id"])] = str(
                                node["name"]
                            )
                        for nested in child.get("inner") or []:
                            if not isinstance(nested, dict):
                                continue
                            decl = nested.get("decl")
                            if (
                                isinstance(decl, dict)
                                and decl.get("id")
                                and decl.get("kind") == "RecordDecl"
                                and not decl.get("name")
                            ):
                                owned_record_to_typedef.setdefault(
                                    str(decl["id"]), str(node["name"])
                                )

        for child in node.get("inner") or []:
            last_file = walk(child, last_file)
        return last_file

    walk(root, None)
    return out, owned_record_to_typedef


def collect_type_uses_from_ast(
    root: Any,
    *,
    package_dir: Path,
    cwd: Path,
    entry_index: int,
    compiler_path: str,
    compiler_id: str,
    compile_commands_digest: str,
    owned_record_to_typedef: Optional[Dict[str, str]] = None,
) -> List[RawTypeUse]:
    """Walk one AST root; collect package-local declaration-bearing type uses."""
    package_dir = package_dir.resolve()
    cwd = cwd.resolve()
    out: List[RawTypeUse] = []
    owned_record_to_typedef = owned_record_to_typedef or {}

    def emit_use(
        *,
        use_kind: str,
        node: Dict[str, Any],
        qual_type: str,
        type_node: Dict[str, Any],
        last_file: Optional[str],
        ancestors: List[Dict[str, Any]],
    ) -> None:
        rel, line, col0, byte_offset, origin, is_local, hint = _node_location(
            node, last_file=last_file, cwd=cwd, package_dir=package_dir
        )
        if not is_local or not rel:
            return

        enclosing_fn: Optional[Dict[str, Any]] = None
        enclosing_rec: Optional[Dict[str, Any]] = None
        enclosing_typedef: Optional[Dict[str, Any]] = None
        for anc in reversed(ancestors):
            if (
                enclosing_fn is None
                and anc.get("kind") == "FunctionDecl"
                and anc.get("name")
            ):
                enclosing_fn = anc
            if enclosing_rec is None and anc.get("kind") == "RecordDecl":
                enclosing_rec = anc
            if (
                enclosing_typedef is None
                and anc.get("kind") == "TypedefDecl"
                and anc.get("name")
            ):
                enclosing_typedef = anc

        owner_kind: Optional[str] = None
        owner_name: Optional[str] = None
        owner_path: Optional[str] = None
        owner_line: Optional[int] = None
        owner_col0: Optional[int] = None
        owner_id: Optional[str] = None
        owner_storage_class: Optional[str] = None
        field_owner_is_typedef = False

        if use_kind == "function_return":
            owner_kind = "function"
            owner_name = str(node.get("name") or "") or None
            owner_path = rel
            owner_line = line
            owner_col0 = col0
            owner_id = str(node["id"]) if node.get("id") else None
            owner_storage_class = (
                str(node["storageClass"]) if node.get("storageClass") else None
            )
        elif use_kind in {"parameter", "local_variable"}:
            owner_kind = "function"
            if enclosing_fn is not None:
                owner_name = str(enclosing_fn.get("name") or "") or None
                owner_id = (
                    str(enclosing_fn["id"]) if enclosing_fn.get("id") else None
                )
                owner_storage_class = (
                    str(enclosing_fn["storageClass"])
                    if enclosing_fn.get("storageClass")
                    else None
                )
                orel, oline, ocol0, _bo, _o, olocal, _h = _node_location(
                    enclosing_fn,
                    last_file=last_file,
                    cwd=cwd,
                    package_dir=package_dir,
                )
                if olocal and orel:
                    owner_path = orel
                    owner_line = oline
                    owner_col0 = ocol0
        elif use_kind == "field":
            # Prefer named RecordDecl; anonymous record typedefs own fields via
            # nested TypedefDecl or ElaboratedType.ownedTagDecl linkage.
            field_owner_node: Optional[Dict[str, Any]] = None
            if enclosing_rec is not None and enclosing_rec.get("name"):
                tag = str(enclosing_rec.get("tagUsed") or "struct")
                owner_kind = "union" if tag == "union" else "struct"
                owner_name = str(enclosing_rec.get("name") or "") or None
                owner_id = (
                    str(enclosing_rec["id"]) if enclosing_rec.get("id") else None
                )
                field_owner_node = enclosing_rec
            elif enclosing_typedef is not None:
                owner_kind = "typedef"
                owner_name = str(enclosing_typedef.get("name") or "") or None
                owner_id = (
                    str(enclosing_typedef["id"])
                    if enclosing_typedef.get("id")
                    else None
                )
                field_owner_node = enclosing_typedef
                field_owner_is_typedef = True
            elif (
                enclosing_rec is not None
                and enclosing_rec.get("id")
                and str(enclosing_rec["id"]) in owned_record_to_typedef
            ):
                owner_kind = "typedef"
                owner_name = owned_record_to_typedef[str(enclosing_rec["id"])]
                owner_id = str(enclosing_rec["id"])
                field_owner_node = enclosing_rec
                field_owner_is_typedef = True
            else:
                owner_kind = "struct"
            if field_owner_node is not None:
                orel, oline, ocol0, _bo, _o, olocal, _h = _node_location(
                    field_owner_node,
                    last_file=last_file,
                    cwd=cwd,
                    package_dir=package_dir,
                )
                if olocal and orel:
                    owner_path = orel
                    owner_line = oline
                    owner_col0 = ocol0
        elif use_kind == "typedef_underlying":
            owner_kind = "typedef"
            owner_name = str(node.get("name") or "") or None
            owner_path = rel
            owner_line = line
            owner_col0 = col0
            owner_id = str(node["id"]) if node.get("id") else None
        elif use_kind == "global_variable":
            owner_kind = None
            owner_name = str(node.get("name") or "") or None

        desug = type_node.get("desugaredQualType")
        alias = type_node.get("typeAliasDeclId")
        out.append(
            RawTypeUse(
                use_kind=use_kind,
                owner_kind=owner_kind,
                owner_name=owner_name,
                owner_source_path=owner_path,
                owner_line=owner_line,
                owner_col0=owner_col0,
                owner_clang_id=owner_id,
                target_qual_type=qual_type,
                target_desugared_qual_type=(
                    str(desug) if desug is not None else None
                ),
                type_alias_decl_id=str(alias) if alias else None,
                source_path=rel,
                line=line,
                col0=col0,
                byte_offset=byte_offset,
                location_origin=origin,
                location_precision="declaration_bearing_node",
                is_package_local=True,
                classification_hint=hint,
                entry_index=entry_index,
                compiler_path=compiler_path,
                compiler_id=compiler_id,
                compile_commands_digest=compile_commands_digest,
                enclosing_function_name=(
                    str(enclosing_fn["name"])
                    if enclosing_fn and enclosing_fn.get("name")
                    else owner_name
                    if use_kind == "function_return"
                    else None
                ),
                enclosing_function_path=owner_path
                if use_kind
                in {"function_return", "parameter", "local_variable"}
                else None,
                enclosing_function_line=owner_line
                if use_kind
                in {"function_return", "parameter", "local_variable"}
                else None,
                enclosing_record_name=owner_name if use_kind == "field" else None,
                enclosing_record_kind=(
                    "typedef"
                    if field_owner_is_typedef
                    else owner_kind
                    if use_kind == "field"
                    else None
                ),
                enclosing_record_path=owner_path if use_kind == "field" else None,
                owner_storage_class=owner_storage_class,
            )
        )

    def walk(
        node: Any,
        last_file: Optional[str],
        ancestors: List[Dict[str, Any]],
    ) -> Optional[str]:
        if not isinstance(node, dict):
            return last_file
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

        kind = node.get("kind")
        type_node = (
            node.get("type") if isinstance(node.get("type"), dict) else None
        )
        qual = (
            str(type_node.get("qualType")).strip()
            if type_node and type_node.get("qualType") is not None
            else ""
        )

        if kind == "FunctionDecl" and node.get("name") and qual:
            ret = split_function_return_qual_type(qual)
            if ret:
                emit_use(
                    use_kind="function_return",
                    node=node,
                    qual_type=ret,
                    type_node=type_node or {},
                    last_file=last_file,
                    ancestors=ancestors,
                )
        elif kind == "ParmVarDecl" and qual:
            emit_use(
                use_kind="parameter",
                node=node,
                qual_type=qual,
                type_node=type_node or {},
                last_file=last_file,
                ancestors=ancestors,
            )
        elif kind == "VarDecl" and qual:
            in_function = any(
                a.get("kind") == "FunctionDecl" for a in ancestors
            )
            emit_use(
                use_kind="local_variable" if in_function else "global_variable",
                node=node,
                qual_type=qual,
                type_node=type_node or {},
                last_file=last_file,
                ancestors=ancestors,
            )
        elif kind == "FieldDecl" and qual:
            emit_use(
                use_kind="field",
                node=node,
                qual_type=qual,
                type_node=type_node or {},
                last_file=last_file,
                ancestors=ancestors,
            )
        elif kind == "TypedefDecl" and node.get("name") and qual:
            emit_use(
                use_kind="typedef_underlying",
                node=node,
                qual_type=qual,
                type_node=type_node or {},
                last_file=last_file,
                ancestors=ancestors,
            )

        next_ancestors = ancestors + [node]
        for child in node.get("inner") or []:
            last_file = walk(child, last_file, next_ancestors)
        return last_file

    walk(root, None, [])
    return out


# ---------------------------------------------------------------------------
# Target / owner resolution
# ---------------------------------------------------------------------------


def _index_matched_types(
    type_report: Dict[str, Any],
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    by: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in type_report.get("matched") or []:
        if not isinstance(row, dict):
            continue
        kind = str(row.get("entity_kind") or "")
        name = str(row.get("name") or "")
        if kind not in {"struct", "enum", "typedef"} or not name:
            continue
        by.setdefault((kind, name), []).append(row)
    return by


def _index_matched_functions(
    function_report: Dict[str, Any],
) -> Dict[str, List[Dict[str, Any]]]:
    by: Dict[str, List[Dict[str, Any]]] = {}
    for row in function_report.get("matched") or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if not name:
            continue
        by.setdefault(name, []).append(row)
    return by


def _rows_at_type_site(
    rows: Sequence[Dict[str, Any]],
    *,
    source_path: Optional[str],
    line: Optional[int],
    col0: Optional[int],
) -> List[Dict[str, Any]]:
    if not source_path or line is None or col0 is None:
        return []
    return [
        row
        for row in rows
        if row.get("source_path") == source_path
        and row.get("matched_site_line") == line
        and row.get("matched_site_col0") == col0
    ]


def _rows_at_function_site(
    rows: Sequence[Dict[str, Any]],
    *,
    source_path: Optional[str],
    line: Optional[int],
    col0: Optional[int],
) -> List[Dict[str, Any]]:
    if not source_path or line is None or col0 is None:
        return []
    return [
        row
        for row in rows
        if row.get("source_path") == source_path
        and row.get("tree_sitter_line") == line
        and row.get("tree_sitter_col") == col0
    ]


def resolve_target(
    use: RawTypeUse,
    *,
    decl_by_id: Dict[Tuple[int, str], PackageTypeDecl],
    matched_types: Dict[Tuple[str, str], List[Dict[str, Any]]],
) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
    """Return (bucket_or_matched, matched_row_or_none, resolver_or_none).

    When the first element is ``matched``, the second is a type-audit matched
    row and the third is the resolver name. Otherwise the first element is a
    residual bucket name.
    """
    if use.classification_hint == "macro_location_unsupported":
        return "macro_location_unsupported", None, None

    qt = use.target_qual_type
    if not qt:
        return "unsupported_type_form", None, None

    # Function-pointer / complex function types as values are unsupported.
    if is_function_pointer_qual_type(qt):
        return "unsupported_type_form", None, None

    # Prefer structural typedef linkage.
    if use.type_alias_decl_id:
        decl = decl_by_id.get((use.entry_index, use.type_alias_decl_id))
        if decl is not None and decl.entity_kind == "typedef":
            rows = matched_types.get(("typedef", decl.name)) or []
            exact = _rows_at_type_site(
                rows,
                source_path=decl.source_path,
                line=decl.line,
                col0=decl.col0,
            )
            if len(exact) == 1:
                return "matched", exact[0], "type_alias_decl_id"
            if len(exact) > 1 or len(rows) > 1:
                return "ambiguous_target", None, None
            # A package typedef id that cannot be bound to the configured
            # declaration site must not fall back to title-only attachment.
            return "target_unresolved", None, None
        if decl is not None and decl.entity_kind != "typedef":
            # Unexpected id kind — do not force.
            return "ambiguous_target", None, None
        # An unknown id is normally external/system. If its spelling names a
        # known package type, keep it as an internal unresolved residual.
        core = strip_pointers_and_qualifiers(qt)
        if is_external_primitive(core):
            return "external_or_system", None, None
        if any(key[1] == core for key in matched_types):
            return "target_unresolved", None, None
        return "external_or_system", None, None

    core = strip_pointers_and_qualifiers(qt)
    if not core:
        return "unsupported_type_form", None, None

    # Nested function types without (*) marker (rare) → unsupported.
    if "(" in core and ")" in core:
        return "unsupported_type_form", None, None

    tag_match = _TAG_RE.match(core)
    if tag_match:
        tag, name = tag_match.group(1), tag_match.group(2)
        if tag == "union":
            return "unsupported_type_form", None, None
        entity_kind = "struct" if tag == "struct" else "enum"
        rows = matched_types.get((entity_kind, name)) or []
        if len(rows) == 1:
            return "matched", rows[0], "exact_tag_spelling"
        if len(rows) > 1:
            return "ambiguous_target", None, None
        # Tag present but not a matched package type entity.
        # Could be incomplete/local anonymous-ish; treat as unresolved if it
        # looks package-ish, else external. Without a package decl index hit
        # by name we cannot claim external vs unresolved — use unresolved
        # only when a package decl of that kind exists unmatched.
        package_names = {
            d.name
            for d in decl_by_id.values()
            if d.entity_kind == entity_kind
        }
        if name in package_names:
            return "target_unresolved", None, None
        return "external_or_system", None, None

    if _IDENT_RE.match(core):
        if is_external_primitive(core):
            return "external_or_system", None, None
        td = matched_types.get(("typedef", core)) or []
        st = matched_types.get(("struct", core)) or []
        en = matched_types.get(("enum", core)) or []
        # C tags occupy a separate namespace. Bare ``T`` can denote a typedef
        # even when ``struct T`` / ``enum T`` also exist; only multiple typedef
        # candidates are ambiguous here.
        if len(td) == 1:
            return "matched", td[0], "unique_typedef_spelling"
        if len(td) > 1:
            return "ambiguous_target", None, None
        if len(st) == 1 and not td and not en:
            # Bare tag name without 'struct' keyword is not forced to struct
            # when only a struct exists — still ambiguous spelling form.
            # Require exact_tag_spelling with struct keyword.
            return "ambiguous_target", None, None
        if len(en) == 1 and not td and not st:
            return "ambiguous_target", None, None
        # Not in matched types: package decl with this name?
        package_hit = any(d.name == core for d in decl_by_id.values())
        if package_hit:
            return "target_unresolved", None, None
        return "external_or_system", None, None

    if is_external_primitive(core.split()[0] if core else ""):
        return "external_or_system", None, None
    if _MULTIWORD_BUILTIN_RE.match(core):
        return "external_or_system", None, None
    return "unsupported_type_form", None, None


def resolve_owner(
    use: RawTypeUse,
    *,
    matched_functions: Dict[str, List[Dict[str, Any]]],
    matched_types: Dict[Tuple[str, str], List[Dict[str, Any]]],
) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """Return (status, owner_title, owner_kind, resolver).

    status is ``ok``, ``unmatched``, or ``unowned``.
    """
    if use.use_kind == "global_variable":
        return "unowned", None, None, None

    if use.use_kind in {"function_return", "parameter", "local_variable"}:
        name = use.enclosing_function_name or use.owner_name
        if not name:
            return "unowned", None, None, None
        rows = matched_functions.get(name) or []
        exact = _rows_at_function_site(
            rows,
            source_path=use.owner_source_path,
            line=use.owner_line,
            col0=use.owner_col0,
        )
        if len(exact) == 1:
            title = str(exact[0].get("tree_sitter_title") or "")
            return (
                ("ok", title, "function", "exact_declaration_site")
                if title
                else ("unmatched", None, None, None)
            )
        # A unique non-static declaration can legitimately be a header
        # prototype for the one package definition. Never use this fallback
        # for internal-linkage functions or duplicate names.
        external_rows = [
            row for row in rows if row.get("storageClass") != "static"
        ]
        same_file_static = [
            row
            for row in rows
            if row.get("storageClass") == "static"
            and row.get("source_path") == use.owner_source_path
        ]
        if use.owner_storage_class == "static" and len(same_file_static) == 1:
            title = str(same_file_static[0].get("tree_sitter_title") or "")
            return (
                (
                    "ok",
                    title,
                    "function",
                    "unique_internal_function_name_same_file",
                )
                if title
                else ("unmatched", None, None, None)
            )
        if use.owner_storage_class != "static" and len(external_rows) == 1:
            title = str(external_rows[0].get("tree_sitter_title") or "")
            return (
                ("ok", title, "function", "unique_external_function_name")
                if title
                else ("unmatched", None, None, None)
            )
        return "unmatched", None, None, None

    if use.use_kind == "field":
        name = use.enclosing_record_name
        kind = use.enclosing_record_kind or "struct"
        if kind == "union" or not name:
            return "unmatched", None, None, None
        if kind == "typedef":
            rows = matched_types.get(("typedef", name)) or []
            owner_kind = "typedef"
        else:
            rows = matched_types.get(("struct", name)) or []
            owner_kind = "struct"
        exact = _rows_at_type_site(
            rows,
            source_path=use.owner_source_path,
            line=use.owner_line,
            col0=use.owner_col0,
        )
        if len(exact) == 1:
            title = str(exact[0].get("tree_sitter_title") or "")
            return (
                ("ok", title, owner_kind, "exact_declaration_site")
                if title
                else ("unmatched", None, None, None)
            )
        if kind == "typedef" and use.owner_source_path and use.owner_line is not None:
            owned_site = [
                row
                for row in rows
                if row.get("source_path") == use.owner_source_path
                and row.get("matched_site_line") == use.owner_line
            ]
            if len(owned_site) == 1:
                title = str(owned_site[0].get("tree_sitter_title") or "")
                return (
                    ("ok", title, owner_kind, "owned_tag_typedef_site")
                    if title
                    else ("unmatched", None, None, None)
                )
        return "unmatched", None, None, None

    if use.use_kind == "typedef_underlying":
        name = use.owner_name
        if not name:
            return "unmatched", None, None, None
        rows = matched_types.get(("typedef", name)) or []
        exact = _rows_at_type_site(
            rows,
            source_path=use.owner_source_path,
            line=use.owner_line,
            col0=use.owner_col0,
        )
        if len(exact) == 1:
            title = str(exact[0].get("tree_sitter_title") or "")
            return (
                ("ok", title, "typedef", "exact_declaration_site")
                if title
                else ("unmatched", None, None, None)
            )
        return "unmatched", None, None, None

    return "unowned", None, None, None


# ---------------------------------------------------------------------------
# Dedup + classification
# ---------------------------------------------------------------------------


def _dedup_key(
    use: RawTypeUse,
    bucket: str,
    resolver: Optional[str],
    owner_title: Optional[str],
    target_title: Optional[str],
    target_kind: Optional[str],
    target_name: Optional[str],
) -> Tuple:
    return (
        bucket,
        use.use_kind,
        owner_title or "",
        use.owner_kind or "",
        use.owner_name or "",
        target_title or "",
        target_kind or "",
        target_name or "",
        use.target_qual_type,
        use.source_path or "",
        use.line if use.line is not None else -1,
        use.col0 if use.col0 is not None else -1,
        use.byte_offset if use.byte_offset is not None else -1,
        resolver or "",
        use.location_precision,
    )


def _merge_provenance(
    existing: Dict[str, Any],
    use: RawTypeUse,
) -> None:
    indices = set(existing.get("entry_indices") or [])
    indices.add(use.entry_index)
    existing["entry_indices"] = sorted(indices)
    compilers = {
        (
            c.get("compiler_path"),
            c.get("compiler_id"),
            c.get("compile_commands_digest"),
        ): c
        for c in (existing.get("compilers") or [])
        if isinstance(c, dict)
    }
    key = (use.compiler_path, use.compiler_id, use.compile_commands_digest)
    compilers[key] = {
        "compiler_path": use.compiler_path,
        "compiler_id": use.compiler_id,
        "compile_commands_digest": use.compile_commands_digest,
    }
    existing["compilers"] = [
        compilers[k] for k in sorted(compilers)
    ]
    if len(existing["compilers"]) == 1:
        only = existing["compilers"][0]
        existing["compiler_path"] = only["compiler_path"]
        existing["compiler_id"] = only["compiler_id"]
    else:
        existing["compiler_path"] = None
        existing["compiler_id"] = None
    digests = {c["compile_commands_digest"] for c in existing["compilers"]}
    existing["compile_commands_digest"] = (
        next(iter(digests)) if len(digests) == 1 else None
    )


def classify_and_dedup(
    uses: Sequence[RawTypeUse],
    *,
    decl_by_id: Dict[Tuple[int, str], PackageTypeDecl],
    matched_types: Dict[Tuple[str, str], List[Dict[str, Any]]],
    matched_functions: Dict[str, List[Dict[str, Any]]],
    digest: str,
) -> Dict[str, List[Dict[str, Any]]]:
    buckets: Dict[str, Dict[Tuple, Dict[str, Any]]] = {
        k: {} for k in _ALL_BUCKETS
    }

    for use in uses:
        if use.compile_commands_digest != digest:
            raise ClangTypeUseAuditError(
                "type-use observation digest disagrees with capture"
            )
        target_status, target_row, resolver = resolve_target(
            use, decl_by_id=decl_by_id, matched_types=matched_types
        )
        if target_status != "matched":
            bucket = target_status
            target_title = None
            target_kind = None
            target_name = None
            # Still record owner fields when known, without inventing titles.
            (
                owner_status,
                owner_title,
                _owner_kind,
                owner_resolver,
            ) = resolve_owner(
                use,
                matched_functions=matched_functions,
                matched_types=matched_types,
            )
            # Macro residual wins even if target also residual.
            if use.classification_hint == "macro_location_unsupported":
                bucket = "macro_location_unsupported"
        else:
            assert target_row is not None and resolver is not None
            target_title = str(target_row.get("tree_sitter_title") or "")
            target_kind = str(target_row.get("entity_kind") or "")
            target_name = str(target_row.get("name") or "")
            (
                owner_status,
                owner_title,
                _owner_kind,
                owner_resolver,
            ) = resolve_owner(
                use,
                matched_functions=matched_functions,
                matched_types=matched_types,
            )
            if owner_status == "unowned":
                bucket = "unowned_context"
            elif owner_status == "unmatched":
                bucket = "owner_unmatched"
            else:
                bucket = "matched_internal"

        row: Dict[str, Any] = {
            "classification": bucket,
            "use_kind": use.use_kind,
            "owner_kind": use.owner_kind,
            "owner_name": use.owner_name,
            "owner_tree_sitter_title": owner_title
            if bucket == "matched_internal"
            else (
                owner_title
                if owner_status == "ok" and target_status != "matched"
                else None
            ),
            "owner_resolver": owner_resolver if owner_status == "ok" else None,
            "target_entity_kind": target_kind
            if target_status == "matched"
            else None,
            "target_name": target_name
            if target_status == "matched"
            else (
                strip_pointers_and_qualifiers(use.target_qual_type) or None
            ),
            "target_tree_sitter_title": target_title
            if target_status == "matched"
            else None,
            "qualType": use.target_qual_type,
            "desugaredQualType": use.target_desugared_qual_type,
            "resolver": resolver if target_status == "matched" else None,
            "source_path": use.source_path,
            "line": use.line,
            "col0": use.col0,
            "byte_offset": use.byte_offset,
            "location_origin": use.location_origin,
            "location_precision": use.location_precision,
            "entry_indices": [use.entry_index],
            "compiler_path": use.compiler_path,
            "compiler_id": use.compiler_id,
            "compile_commands_digest": use.compile_commands_digest,
            "compilers": [
                {
                    "compiler_path": use.compiler_path,
                    "compiler_id": use.compiler_id,
                    "compile_commands_digest": use.compile_commands_digest,
                }
            ],
        }
        # For matched_internal, always expose owner title.
        if bucket == "matched_internal":
            row["owner_tree_sitter_title"] = owner_title
            row["target_tree_sitter_title"] = target_title
            row["target_entity_kind"] = target_kind
            row["target_name"] = target_name
            row["resolver"] = resolver

        key = _dedup_key(
            use,
            bucket,
            resolver,
            row.get("owner_tree_sitter_title"),
            row.get("target_tree_sitter_title"),
            row.get("target_entity_kind"),
            row.get("target_name"),
        )
        existing = buckets[bucket].get(key)
        if existing is None:
            buckets[bucket][key] = row
        else:
            _merge_provenance(existing, use)

    def sort_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            rows,
            key=lambda r: (
                str(r.get("use_kind") or ""),
                str(r.get("source_path") or ""),
                int(r.get("line") or 0),
                int(r.get("col0") or 0),
                str(r.get("owner_tree_sitter_title") or ""),
                str(r.get("target_tree_sitter_title") or ""),
                str(r.get("qualType") or ""),
                _canonical_json(r),
            ),
        )

    return {
        name: sort_rows(list(bucket_map.values()))
        for name, bucket_map in buckets.items()
    }


# ---------------------------------------------------------------------------
# Report builders
# ---------------------------------------------------------------------------


def _normalize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    text = json.dumps(report, sort_keys=True, separators=(",", ":"))
    return json.loads(text)


def audit_to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, indent=2) + "\n"


def build_type_use_audit_from_capture(
    capture: Any,
    *,
    function_report: Optional[Dict[str, Any]] = None,
    type_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the type-use audit from an in-memory capture.

    Zero compiler invocations; zero compile_commands reloads; never mutates
    the capture or AST roots.

    Optional ``function_report`` / ``type_report`` must already have been built
    from the same capture (validated against it). When omitted, they are built
    here — standalone CLI behavior remains byte-identical.
    """
    from graphrag_code.c_clang_ast_capture import (  # type: ignore
        ClangAstCaptureError,
        ClangAstPackageCapture,
        assert_audit_report_matches_capture,
        validate_clang_ast_capture,
    )

    if not isinstance(capture, ClangAstPackageCapture):
        raise ClangTypeUseAuditError(
            "build_type_use_audit_from_capture requires a ClangAstPackageCapture"
        )
    try:
        validate_clang_ast_capture(capture)
    except ClangAstCaptureError as e:
        raise ClangTypeUseAuditError(str(e)) from e

    package_dir = capture.package_dir
    digest = capture.compile_commands_digest

    # Snapshot AST identity before pure work (mutation guard for tests).
    ast_fingerprint_before = [
        id(entry.ast_root) for entry in capture.entries
    ]

    try:
        if function_report is None:
            function_report = build_function_audit_from_capture(capture)
        else:
            if not isinstance(function_report, dict):
                raise ClangTypeUseAuditError(
                    "function_report must be a dict when provided"
                )
            assert_audit_report_matches_capture(
                function_report,
                capture,
                context="type-use function_report",
            )
            if str(function_report.get("mode") or "") != "clang_ast_json_audit":
                raise ClangTypeUseAuditError(
                    f"function_report has unexpected mode "
                    f"{function_report.get('mode')!r}"
                )
        if type_report is None:
            type_report = build_type_declaration_audit_from_capture(capture)
        else:
            if not isinstance(type_report, dict):
                raise ClangTypeUseAuditError(
                    "type_report must be a dict when provided"
                )
            assert_audit_report_matches_capture(
                type_report,
                capture,
                context="type-use type_report",
            )
            if (
                str(type_report.get("mode") or "")
                != "clang_ast_json_type_declaration_audit"
            ):
                raise ClangTypeUseAuditError(
                    f"type_report has unexpected mode {type_report.get('mode')!r}"
                )
    except ClangAstCaptureError as e:
        raise ClangTypeUseAuditError(str(e)) from e
    except (ClangAstAuditError, ClangTypeAuditError) as e:
        raise ClangTypeUseAuditError(str(e)) from e

    matched_functions = _index_matched_functions(function_report)
    matched_types = _index_matched_types(type_report)

    decl_by_id: Dict[Tuple[int, str], PackageTypeDecl] = {}
    all_uses: List[RawTypeUse] = []
    translation_units: List[Dict[str, Any]] = []

    for ent in capture.entries:
        decls, owned_map = collect_package_type_decls_from_ast(
            ent.ast_root, package_dir=package_dir, cwd=ent.cwd
        )
        # Clang ids are unique only within one AST dump, so entry index is part
        # of the identity. Reusing a bare id across TUs would misbind aliases.
        for clang_id, decl in decls.items():
            decl_by_id[(ent.entry_index, clang_id)] = decl
        uses = collect_type_uses_from_ast(
            ent.ast_root,
            package_dir=package_dir,
            cwd=ent.cwd,
            entry_index=ent.entry_index,
            compiler_path=ent.compiler_path,
            compiler_id=ent.compiler_id,
            compile_commands_digest=digest,
            owned_record_to_typedef=owned_map,
        )
        all_uses.extend(uses)
        tu_local = path_is_under(ent.tu_path, package_dir)
        tu_file = (
            package_relative_posix(ent.tu_path, package_dir)
            if tu_local
            else None
        )
        translation_units.append(
            {
                "entry_index": ent.entry_index,
                "file": tu_file,
                "package_local": tu_local,
                "compiler_path": ent.compiler_path,
                "compiler_id": ent.compiler_id,
                "n_type_uses_observed": len(uses),
            }
        )

    if [id(entry.ast_root) for entry in capture.entries] != ast_fingerprint_before:
        raise ClangTypeUseAuditError(
            "type-use audit mutated capture AST roots"
        )

    bucket_rows = classify_and_dedup(
        all_uses,
        decl_by_id=decl_by_id,
        matched_types=matched_types,
        matched_functions=matched_functions,
        digest=digest,
    )
    counts = {k: len(bucket_rows[k]) for k in _ALL_BUCKETS}
    counts["type_uses_raw_observations"] = len(all_uses)
    counts["type_uses_deduped_total"] = sum(counts[k] for k in _ALL_BUCKETS)
    counts["function_audit_matched"] = len(function_report.get("matched") or [])
    counts["type_declaration_audit_matched"] = len(
        type_report.get("matched") or []
    )

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
        "column_convention": {
            "location_precision": (
                "declaration_bearing_node — coordinates come from the "
                "FunctionDecl/ParmVarDecl/VarDecl/FieldDecl/TypedefDecl "
                "loc or range.begin, not an exact type-token span"
            ),
            "clang_column": "1-based when present; stored col0 is 0-based",
            "byte_offset": (
                "range.begin.offset when present; not a type-token offset"
            ),
        },
        "fail_on_mismatch_policy": {
            "exit_1_buckets": list(_FAIL_ON_MISMATCH_BUCKETS),
            "observation_only_buckets": list(_OBSERVATION_ONLY_BUCKETS),
            "note": (
                "external_or_system, unsupported_type_form, and unowned_context "
                "do not by themselves cause --fail-on-mismatch to exit 1"
            ),
        },
        "resolvers": list(_RESOLVERS),
        "owner_resolvers": list(_OWNER_RESOLVERS),
        "use_kinds": list(_USE_KINDS),
        "counts": counts,
        **{k: bucket_rows[k] for k in _ALL_BUCKETS},
        "limitations": [
            "Standalone CLI is diagnostic only; optional --clang-type-uses may publish aggregated uses_type edges",
            "Locations are declaration-bearing nodes, not proven type-token spans",
            "Target resolution prefers typeAliasDeclId; spelling is unique-only",
            "C tag names stay distinct: bare names resolve only as unique typedef spellings",
            "Function return types are parsed from FunctionProto qualType text",
            "Function-pointer and unsupported type forms remain residual",
            "Owners come from function/type declaration audits (not re-matched)",
            "Not layout/ABI, not multi-config, not points-to, not C++",
            "Header observations are deduplicated across compile entries",
        ],
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }

    for key in _ALL_BUCKETS:
        if counts[key] != len(report[key]):
            raise ClangTypeUseAuditError(
                f"internal count/list mismatch for {key}: "
                f"count={counts[key]} rows={len(report[key])}"
            )

    normalized = _normalize_report(report)
    try:
        assert_audit_report_matches_capture(
            normalized, capture, context="type-use audit"
        )
    except ClangAstCaptureError as e:
        raise ClangTypeUseAuditError(str(e)) from e
    return normalized


def run_clang_type_use_audit(
    package_dir: Path,
    *,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Run the type-use audit (one capture, then pure builder)."""
    from graphrag_code.c_clang_ast_capture import (  # type: ignore
        ClangAstCaptureError,
        capture_clang_ast_package,
    )

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout <= 0
    ):
        raise ClangTypeUseAuditError(
            f"timeout must be a positive integer, got {timeout!r}"
        )
    try:
        capture = capture_clang_ast_package(package_dir, timeout=timeout)
    except ClangAstCaptureError as e:
        raise ClangTypeUseAuditError(str(e)) from e
    return build_type_use_audit_from_capture(capture)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Clang AST JSON type-use evidence audit (diagnostic only; "
            "no uses_type edges; no BYOG mutation)."
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
            "Exit 1 when owner_unmatched, target_unresolved, ambiguous_target, "
            "or macro_location_unsupported counts are non-zero. "
            "external_or_system, unsupported_type_form, and unowned_context "
            "alone do not fail."
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
        report = run_clang_type_use_audit(args.package, timeout=args.timeout)
    except ClangTypeUseAuditError as e:
        print(f"c_clang_type_use_audit: {e}", file=sys.stderr)
        return 2
    except ClangAstAuditError as e:
        print(f"c_clang_type_use_audit: {e}", file=sys.stderr)
        return 2
    except CompilerOverlayError as e:
        print(f"c_clang_type_use_audit: {e}", file=sys.stderr)
        return 2

    text = audit_to_json(report)
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        except OSError as e:
            print(
                f"c_clang_type_use_audit: failed to write output: {e}",
                file=sys.stderr,
            )
            return 2
    else:
        sys.stdout.write(text)

    if args.fail_on_mismatch:
        counts = report.get("counts") or {}
        residual = sum(
            int(counts.get(k) or 0) for k in _FAIL_ON_MISMATCH_BUCKETS
        )
        if residual:
            print(
                "c_clang_type_use_audit: --fail-on-mismatch: "
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
