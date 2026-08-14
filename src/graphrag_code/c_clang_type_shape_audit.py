#!/usr/bin/env python
"""Clang AST JSON type-shape audit (diagnostic only; member inventory).

Compares **ordered member names** of package-local named complete structs and
enums that the configured type-declaration audit already matched, against
tree-sitter-c direct field/enumerator inventories at the exact matched
declaration site.

This is **member-shape evidence only**. It is not ABI/layout evidence and does
not claim size, alignment, offsets, calling convention, Rust representation
compatibility, or FFI safety. Raw Clang type spellings, enum values, and
bit-field widths are diagnostic evidence fields only.

This module itself performs no BYOG mutation and creates no graph entities or
relationships. It is the single shared builder behind both the standalone
diagnostic CLI and the optional ``--clang-type-shapes`` evidence overlay in
``c_clang_type_shapes.py``.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from graphrag_code.c_clang_ast_audit import (  # type: ignore
    ClangAstAuditError,
    _normalize_path_token,
    resolve_function_location,
)
from graphrag_code.c_clang_type_audit import (  # type: ignore
    ClangTypeAuditError,
    build_type_declaration_audit_from_capture,
)
from graphrag_code.c_compiler_common import path_is_under  # type: ignore
from graphrag_code.c_identities import package_relative_posix  # type: ignore
from graphrag_code.extract_c import (  # type: ignore
    TypeShapeMember,
    collect_type_shape_members_at_site,
)

MODE = "clang_ast_json_type_shape_audit"
CONFIDENCE_BOUNDARY = (
    "Type-shape classifications compare ordered direct member *names* of "
    "configured matched struct/enum declarations only. Clang qualType, "
    "desugaredQualType, enum integer values, bit-field widths, and locations "
    "are diagnostic evidence relative to the recorded Clang + "
    "compile_commands.json configuration — not size/alignment/offset/ABI "
    "claims, not Rust representation compatibility, not FFI safety, not "
    "layout proof, not multi-config coverage, and not C++. Typedef aliases "
    "are not independent shapes. Nested record bodies are not flattened."
)

# --fail-on-mismatch exit 1 when any of these are nonzero.
_FAIL_ON_MISMATCH_BUCKETS = (
    "tree_sitter_only_members",
    "clang_only_members",
    "member_order_mismatch",
    "duplicate_or_ambiguous_members",
    "macro_location_unsupported",
    "owner_unmatched",
)

# Explicit residuals that do not alone fail --fail-on-mismatch.
_OBSERVATION_ONLY_BUCKETS = (
    "unsupported_member_form",
    "outside_package_declarations",
)

_ALL_BUCKETS = (
    "matched_shape",
    *_FAIL_ON_MISMATCH_BUCKETS,
    *_OBSERVATION_ONLY_BUCKETS,
)

_SHAPE_OWNER_KINDS = frozenset({"struct", "enum"})


class ClangTypeShapeAuditError(ClangAstAuditError):
    """Raised when the type-shape audit cannot run honestly."""


def audit_to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, indent=2) + "\n"


def _normalize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    text = json.dumps(report, sort_keys=True, separators=(",", ":"))
    return json.loads(text)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _member_to_dict(m: TypeShapeMember) -> Dict[str, Any]:
    return {
        "name": m.name,
        "order": m.order,
        "form": m.form,
        "is_bitfield": m.is_bitfield,
        "bit_width": m.bit_width,
        "line": m.line,
        "col0": m.col0,
        "span": m.span,
        "residual": m.residual,
    }


def _named_sequence(members: Sequence[Dict[str, Any]]) -> List[str]:
    """Ordered comparison names (named members only; residuals excluded)."""
    out: List[str] = []
    for m in members:
        name = m.get("name")
        residual = m.get("residual")
        if residual:
            continue
        if isinstance(name, str) and name:
            out.append(name)
    return out


def _clang_col_to_zero_based(col1: Optional[int]) -> Optional[int]:
    if col1 is None:
        return None
    if isinstance(col1, bool) or not isinstance(col1, int) or col1 < 1:
        return None
    return col1 - 1


def _type_props(node: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    t = node.get("type") if isinstance(node.get("type"), dict) else {}
    qual = t.get("qualType")
    desug = t.get("desugaredQualType")
    return (
        str(qual) if qual is not None else None,
        str(desug) if desug is not None else None,
    )


def _integer_from_clang_node(node: Dict[str, Any]) -> Optional[int]:
    """Extract a signed integer when Clang JSON exposes one directly."""
    for key in ("value", "valueAsString", "intValue"):
        raw = node.get(key)
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                return int(raw.strip(), 0)
            except ValueError:
                continue
    return None


def _clang_bit_width(field: Dict[str, Any]) -> Optional[int]:
    if not field.get("isBitfield"):
        return None
    direct = _integer_from_clang_node(field)
    if direct is not None:
        return direct
    for child in field.get("inner") or []:
        if not isinstance(child, dict):
            continue
        if child.get("kind") in {
            "IntegerLiteral",
            "ConstantExpr",
            "ImplicitCastExpr",
        }:
            val = _integer_from_clang_node(child)
            if val is not None:
                return val
            for grand in child.get("inner") or []:
                if isinstance(grand, dict):
                    val = _integer_from_clang_node(grand)
                    if val is not None:
                        return val
    return None


def _clang_enum_value(const: Dict[str, Any]) -> Optional[int]:
    direct = _integer_from_clang_node(const)
    if direct is not None:
        return direct
    for child in const.get("inner") or []:
        if not isinstance(child, dict):
            continue
        if child.get("kind") in {
            "IntegerLiteral",
            "ConstantExpr",
            "UnaryOperator",
            "ImplicitCastExpr",
        }:
            val = _integer_from_clang_node(child)
            if val is not None:
                return val
            for grand in child.get("inner") or []:
                if isinstance(grand, dict):
                    val = _integer_from_clang_node(grand)
                    if val is not None:
                        return val
    return None


def _loc_coords(
    loc: Any,
    range_begin: Any,
    last_file: Optional[str],
    *,
    cwd: Path,
    package_dir: Path,
) -> Tuple[
    Optional[str],
    Optional[int],
    Optional[int],
    str,
    Optional[str],
    Optional[str],
]:
    """Return (package_rel_path, line, col0, origin, spelling_path, expansion_path)."""
    primary, spelling, expansion = resolve_function_location(
        loc if isinstance(loc, dict) else None,
        last_file=last_file,
        range_begin=range_begin if isinstance(range_begin, dict) else None,
    )
    start_file = (
        str(range_begin.get("file"))
        if isinstance(range_begin, dict) and range_begin.get("file")
        else primary.file
    )
    file_for_norm = start_file or primary.file
    prim_rel, _prim_abs, prim_local = _normalize_path_token(
        file_for_norm, cwd=cwd, package_dir=package_dir
    )
    spell_rel, _, _ = _normalize_path_token(
        spelling.file, cwd=cwd, package_dir=package_dir
    )
    exp_rel, _, _ = _normalize_path_token(
        expansion.file, cwd=cwd, package_dir=package_dir
    )
    line = primary.line
    col0 = _clang_col_to_zero_based(primary.col)
    # Prefer range.begin line/col when present (declaration start).
    if isinstance(range_begin, dict):
        if isinstance(range_begin.get("line"), int) and not isinstance(
            range_begin.get("line"), bool
        ):
            line = int(range_begin["line"])
        if isinstance(range_begin.get("col"), int) and not isinstance(
            range_begin.get("col"), bool
        ):
            col0 = _clang_col_to_zero_based(int(range_begin["col"]))
    origin = str(primary.origin or "direct")
    path = prim_rel if prim_local else None
    return path, line, col0, origin, spell_rel, exp_rel


def _inventory_clang_struct_fields(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    members: List[Dict[str, Any]] = []
    order = 0
    for child in node.get("inner") or []:
        if not isinstance(child, dict):
            continue
        kind = child.get("kind")
        if kind != "FieldDecl":
            # Nested RecordDecl / FunctionDecl etc. are not parent fields.
            if kind in {
                "RecordDecl",
                "CXXRecordDecl",
                "EnumDecl",
                "FunctionDecl",
                "CXXMethodDecl",
            }:
                continue
            if kind is not None and not str(kind).endswith("Comment"):
                # Unexpected injected children — residual, not a named field.
                members.append(
                    {
                        "name": None,
                        "order": order,
                        "form": "unsupported",
                        "is_bitfield": False,
                        "bit_width": None,
                        "qualType": None,
                        "desugaredQualType": None,
                        "line": None,
                        "col0": None,
                        "location_origin": None,
                        "residual": "unsupported_member_form",
                        "clang_kind": kind,
                    }
                )
                order += 1
            continue
        name_raw = child.get("name")
        name = str(name_raw) if isinstance(name_raw, str) else None
        is_bitfield = bool(child.get("isBitfield"))
        width = _clang_bit_width(child)
        qual, desug = _type_props(child)
        loc = child.get("loc") if isinstance(child.get("loc"), dict) else {}
        line = loc.get("line") if isinstance(loc.get("line"), int) else None
        col1 = loc.get("col") if isinstance(loc.get("col"), int) else None
        col0 = _clang_col_to_zero_based(col1)
        residual = None
        form = "field"
        if not name:
            if is_bitfield:
                form = "unnamed_bitfield"
                residual = "unnamed_bitfield"
                name = None
            else:
                form = "anonymous"
                residual = "anonymous_member"
                name = None
        members.append(
            {
                "name": name if name else None,
                "order": order,
                "form": form,
                "is_bitfield": is_bitfield,
                "bit_width": width,
                "qualType": qual,
                "desugaredQualType": desug,
                "line": line,
                "col0": col0,
                "location_origin": "direct",
                "residual": residual,
                "clang_kind": "FieldDecl",
            }
        )
        order += 1
    return members


def _inventory_clang_enum_constants(node: Dict[str, Any]) -> List[Dict[str, Any]]:
    members: List[Dict[str, Any]] = []
    order = 0
    for child in node.get("inner") or []:
        if not isinstance(child, dict):
            continue
        if child.get("kind") != "EnumConstantDecl":
            continue
        name_raw = child.get("name")
        name = str(name_raw) if isinstance(name_raw, str) and name_raw else None
        value = _clang_enum_value(child)
        loc = child.get("loc") if isinstance(child.get("loc"), dict) else {}
        line = loc.get("line") if isinstance(loc.get("line"), int) else None
        col1 = loc.get("col") if isinstance(loc.get("col"), int) else None
        residual = None if name else "unsupported_member_form"
        members.append(
            {
                "name": name,
                "order": order,
                "form": "enumerator" if name else "unsupported",
                "is_bitfield": False,
                "bit_width": None,
                "enum_value": value,
                "qualType": _type_props(child)[0],
                "desugaredQualType": _type_props(child)[1],
                "line": line,
                "col0": _clang_col_to_zero_based(col1),
                "location_origin": "direct",
                "residual": residual,
                "clang_kind": "EnumConstantDecl",
            }
        )
        order += 1
    return members


def _collect_clang_shape_declarations(
    root: Any,
    *,
    package_dir: Path,
    cwd: Path,
    entry_index: int,
    compiler_path: str,
    compiler_id: Optional[str],
    compile_commands_digest: str,
) -> List[Dict[str, Any]]:
    """Walk one AST; collect package-local named complete struct/enum shapes."""
    package_dir = package_dir.resolve()
    cwd = cwd.resolve()
    out: List[Dict[str, Any]] = []

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

        rng = node.get("range") if isinstance(node.get("range"), dict) else None
        range_begin = rng.get("begin") if isinstance(rng, dict) else None
        kind = node.get("kind")

        if kind in {"RecordDecl", "EnumDecl"}:
            path, line, col0, origin, spell, exp = _loc_coords(
                loc,
                range_begin,
                last_file,
                cwd=cwd,
                package_dir=package_dir,
            )
            if path and isinstance(loc, dict) and loc.get("file"):
                last_file = str(loc.get("file") or last_file)

            name_raw = node.get("name")
            name = str(name_raw) if isinstance(name_raw, str) and name_raw else None
            is_union = (
                kind == "RecordDecl" and str(node.get("tagUsed") or "") == "union"
            )
            is_struct = (
                kind == "RecordDecl" and str(node.get("tagUsed") or "") == "struct"
            )
            is_enum = kind == "EnumDecl"
            is_complete = bool(node.get("completeDefinition")) if kind == "RecordDecl" else True
            if is_enum:
                is_complete = any(
                    isinstance(c, dict) and c.get("kind") == "EnumConstantDecl"
                    for c in (node.get("inner") or [])
                )

            package_local = bool(path)
            outside = not package_local

            # Only inventory shapes we might match; still record outside/residual
            # owners for explicit buckets when referenced.
            if is_struct and is_complete and name and package_local:
                members = _inventory_clang_struct_fields(node)
                out.append(
                    {
                        "entity_kind": "struct",
                        "name": name,
                        "source_path": path,
                        "line": line,
                        "col0": col0,
                        "location_origin": origin,
                        "spelling_path": spell,
                        "expansion_path": exp,
                        "is_package_local": True,
                        "is_complete": True,
                        "members": members,
                        "entry_index": entry_index,
                        "compiler_path": compiler_path,
                        "compiler_id": compiler_id,
                        "compile_commands_digest": compile_commands_digest,
                    }
                )
            elif is_enum and is_complete and name and package_local:
                members = _inventory_clang_enum_constants(node)
                out.append(
                    {
                        "entity_kind": "enum",
                        "name": name,
                        "source_path": path,
                        "line": line,
                        "col0": col0,
                        "location_origin": origin,
                        "spelling_path": spell,
                        "expansion_path": exp,
                        "is_package_local": True,
                        "is_complete": True,
                        "members": members,
                        "entry_index": entry_index,
                        "compiler_path": compiler_path,
                        "compiler_id": compiler_id,
                        "compile_commands_digest": compile_commands_digest,
                    }
                )
            elif outside and name and (is_struct or is_enum) and is_complete:
                out.append(
                    {
                        "entity_kind": "struct" if is_struct else "enum",
                        "name": name,
                        "source_path": path,
                        "line": line,
                        "col0": col0,
                        "location_origin": origin,
                        "is_package_local": False,
                        "is_complete": is_complete,
                        "members": [],
                        "entry_index": entry_index,
                        "compiler_path": compiler_path,
                        "compiler_id": compiler_id,
                        "compile_commands_digest": compile_commands_digest,
                        "classification_hint": "outside_package",
                    }
                )

        for child in node.get("inner") or []:
            last_file = walk(child, last_file)
        return last_file

    walk(root, None)
    return out


def _clang_identity_key(obs: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        str(obs.get("entity_kind") or ""),
        str(obs.get("source_path") or ""),
        str(obs.get("name") or ""),
        int(obs.get("line") or 0),
        int(obs.get("col0") or 0),
    )


def _matched_identity_key(row: Dict[str, Any]) -> Tuple[Any, ...]:
    return (
        str(row.get("entity_kind") or ""),
        str(row.get("source_path") or ""),
        str(row.get("name") or ""),
        int(row.get("clang_line") or row.get("line") or 0),
        int(
            row.get("clang_col0")
            if row.get("clang_col0") is not None
            else (row.get("col0") or 0)
        ),
    )


def _semantic_member_inventory(members: Sequence[Dict[str, Any]]) -> str:
    """Canonical semantic evidence used to compare compile-entry views.

    Source locations can be represented differently when the same header is
    reached from different translation units.  Member order, form, type
    spelling, enum value, and bit-field evidence must nevertheless agree; a
    disagreement is an explicit ambiguity rather than first-entry-wins.
    """
    evidence_keys = (
        "name",
        "order",
        "form",
        "is_bitfield",
        "bit_width",
        "enum_value",
        "qualType",
        "desugaredQualType",
        "residual",
        "clang_kind",
    )
    return _canonical_json(
        [{key: member.get(key) for key in evidence_keys} for member in members]
    )


def _classify_shape(
    *,
    ts_members: List[Dict[str, Any]],
    clang_members: List[Dict[str, Any]],
    multi_entry_conflict: bool,
    site_error: Optional[str],
    location_origin: str,
) -> Tuple[str, Dict[str, Any]]:
    """Return (classification, extra detail)."""
    if site_error == "site_not_found" or site_error == "io_error":
        return "owner_unmatched", {"site_error": site_error}
    if site_error == "kind_mismatch":
        return "owner_unmatched", {"site_error": site_error}

    if location_origin not in {"direct", "same_file_spelling_expansion"}:
        # Macro multi-file disagreement is not guessed into a match.
        if "macro" in location_origin or location_origin in {
            "spelling",
            "expansion",
            "macro_disagreement",
        }:
            return "macro_location_unsupported", {
                "location_origin": location_origin
            }

    if multi_entry_conflict:
        return "duplicate_or_ambiguous_members", {
            "reason": "conflicting clang member inventories across compile entries"
        }

    ts_named = _named_sequence(ts_members)
    cl_named = _named_sequence(clang_members)

    # Duplicate names on either side.
    if len(ts_named) != len(set(ts_named)) or len(cl_named) != len(set(cl_named)):
        return "duplicate_or_ambiguous_members", {
            "reason": "duplicate member names within one inventory",
            "tree_sitter_names": ts_named,
            "clang_names": cl_named,
        }

    ts_residuals = [m for m in ts_members if m.get("residual")]
    cl_residuals = [m for m in clang_members if m.get("residual")]
    if ts_named == cl_named:
        # Matching the supported names must not silently erase an anonymous or
        # otherwise unsupported direct member in either inventory.
        if ts_residuals or cl_residuals:
            return "unsupported_member_form", {
                "tree_sitter_residuals": [
                    m.get("residual") for m in ts_residuals
                ],
                "clang_residuals": [m.get("residual") for m in cl_residuals],
            }
        # This includes two genuinely empty inventories.
        return "matched_shape", {}

    if sorted(ts_named) == sorted(cl_named):
        return "member_order_mismatch", {
            "tree_sitter_names": ts_named,
            "clang_names": cl_named,
        }

    ts_only = [n for n in ts_named if n not in set(cl_named)]
    cl_only = [n for n in cl_named if n not in set(ts_named)]
    if ts_only and not cl_only:
        return "tree_sitter_only_members", {
            "tree_sitter_only": ts_only,
            "tree_sitter_names": ts_named,
            "clang_names": cl_named,
        }
    if cl_only and not ts_only:
        return "clang_only_members", {
            "clang_only": cl_only,
            "tree_sitter_names": ts_named,
            "clang_names": cl_named,
        }
    # Both sides have exclusive names — treat as ambiguity, not a match.
    return "duplicate_or_ambiguous_members", {
        "reason": "member set mismatch on both sides",
        "tree_sitter_only": ts_only,
        "clang_only": cl_only,
        "tree_sitter_names": ts_named,
        "clang_names": cl_named,
    }


def build_type_shape_audit_from_capture(
    capture: Any,
    *,
    type_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the type-shape audit from an in-memory capture.

    Never invokes the compiler or reloads ``compile_commands.json``.
    Reuses :func:`build_type_declaration_audit_from_capture` for owners.

    An optional ``type_report`` must already have been built from this same
    capture; it is validated against the capture census (package, digest,
    toolchain identity, translation units) before reuse. When omitted it is
    built here, so standalone CLI output stays byte-identical.
    """
    from graphrag_code.c_clang_ast_capture import (  # type: ignore
        ClangAstCaptureError,
        ClangAstPackageCapture,
        assert_audit_report_matches_capture,
        validate_clang_ast_capture,
    )

    if not isinstance(capture, ClangAstPackageCapture):
        raise ClangTypeShapeAuditError(
            "build_type_shape_audit_from_capture requires a ClangAstPackageCapture"
        )
    try:
        validate_clang_ast_capture(capture)
    except ClangAstCaptureError as e:
        raise ClangTypeShapeAuditError(str(e)) from e

    try:
        if type_report is None:
            type_report = build_type_declaration_audit_from_capture(capture)
        else:
            if not isinstance(type_report, dict):
                raise ClangTypeShapeAuditError(
                    "type_report must be a dict when provided"
                )
            assert_audit_report_matches_capture(
                type_report, capture, context="type-shape type_report"
            )
            if (
                str(type_report.get("mode") or "")
                != "clang_ast_json_type_declaration_audit"
            ):
                raise ClangTypeShapeAuditError(
                    f"type_report has unexpected mode {type_report.get('mode')!r}"
                )
    except ClangAstCaptureError as e:
        raise ClangTypeShapeAuditError(str(e)) from e
    except ClangTypeAuditError as e:
        raise ClangTypeShapeAuditError(str(e)) from e

    package_dir = capture.package_dir
    digest = capture.compile_commands_digest

    # Matched owners from the declaration audit (struct/enum only).
    matched_owners = [
        row
        for row in (type_report.get("matched") or [])
        if isinstance(row, dict)
        and str(row.get("entity_kind") or "") in _SHAPE_OWNER_KINDS
    ]

    # Index clang shape observations by declaration identity.
    clang_by_id: Dict[Tuple[Any, ...], List[Dict[str, Any]]] = {}
    outside_rows: List[Dict[str, Any]] = []
    for ent in capture.entries:
        shapes = _collect_clang_shape_declarations(
            ent.ast_root,
            package_dir=package_dir,
            cwd=ent.cwd,
            entry_index=ent.entry_index,
            compiler_path=ent.compiler_path,
            compiler_id=ent.compiler_id,
            compile_commands_digest=digest,
        )
        for shape in shapes:
            if shape.get("classification_hint") == "outside_package":
                outside_rows.append(
                    {
                        "classification": "outside_package_declarations",
                        "entity_kind": shape.get("entity_kind"),
                        "name": shape.get("name"),
                        "source_path": shape.get("source_path"),
                        "line": shape.get("line"),
                        "col0": shape.get("col0"),
                        "entry_indices": [shape.get("entry_index")],
                    }
                )
                continue
            key = _clang_identity_key(shape)
            clang_by_id.setdefault(key, []).append(shape)

    buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k in _ALL_BUCKETS}
    seen_titles: Set[str] = set()

    for owner in matched_owners:
        title = str(owner.get("tree_sitter_title") or "")
        if not title:
            continue
        if title in seen_titles:
            # Declaration audit already entity-level; this guards double emit.
            continue
        seen_titles.add(title)

        entity_kind = str(owner.get("entity_kind") or "")
        source_path = str(owner.get("source_path") or "")
        site_line = int(owner.get("matched_site_line") or owner.get("tree_sitter_line") or 0)
        site_col0 = int(
            owner.get("matched_site_col0")
            if owner.get("matched_site_col0") is not None
            else (owner.get("tree_sitter_col") or 0)
        )

        site_error, ts_raw = collect_type_shape_members_at_site(
            package_dir,
            source_path=source_path,
            entity_kind=entity_kind,
            line=site_line,
            col0=site_col0,
        )
        ts_members = [_member_to_dict(m) for m in ts_raw]

        id_key = _matched_identity_key(owner)
        clang_obs = list(clang_by_id.get(id_key) or [])

        multi_entry_conflict = False
        entry_indices: List[int] = []
        clang_members: List[Dict[str, Any]] = []
        compilers: List[Dict[str, str]] = []
        location_origin = str(owner.get("location_origin") or "direct")

        if not clang_obs:
            # Owner matched by type audit but shape walk could not re-find the
            # declaration — fail closed as owner_unmatched for shape purposes.
            classification = "owner_unmatched"
            detail = {"reason": "no_clang_shape_observation_for_matched_owner"}
            clang_members = []
        else:
            # Aggregate observations.  The declaration identity is exact, and
            # all semantic member evidence must agree across compile entries.
            sorted_observations = sorted(
                clang_obs,
                key=lambda o: (
                    int(o.get("entry_index") or 0),
                    _canonical_json(o.get("members") or []),
                ),
            )
            inventories: List[str] = []
            for obs in sorted_observations:
                idx = obs.get("entry_index")
                if isinstance(idx, int) and not isinstance(idx, bool):
                    entry_indices.append(idx)
                members = list(obs.get("members") or [])
                inventories.append(_semantic_member_inventory(members))
                cpath = obs.get("compiler_path")
                cid = obs.get("compiler_id")
                if isinstance(cpath, str) and isinstance(cid, str):
                    compilers.append(
                        {
                            "compiler_path": cpath,
                            "compiler_id": cid,
                            "compile_commands_digest": str(
                                obs.get("compile_commands_digest") or digest
                            ),
                        }
                    )
                location_origin = str(
                    obs.get("location_origin") or location_origin
                )
            clang_members = list(sorted_observations[0].get("members") or [])
            if not inventories or not all(
                inventory == inventories[0] for inventory in inventories
            ):
                multi_entry_conflict = True

            classification, detail = _classify_shape(
                ts_members=ts_members,
                clang_members=clang_members,
                multi_entry_conflict=multi_entry_conflict,
                site_error=site_error,
                location_origin=location_origin,
            )

        # Deduplicate entry indices / compilers.
        entry_indices = sorted(set(entry_indices))
        comp_map = {
            (c["compiler_path"], c["compiler_id"], c["compile_commands_digest"]): c
            for c in compilers
        }
        compilers_sorted = [comp_map[k] for k in sorted(comp_map)]

        row = {
            "classification": classification,
            "entity_kind": entity_kind,
            "name": owner.get("name"),
            "source_path": source_path,
            "tree_sitter_title": title,
            "matched_site_span": owner.get("matched_site_span"),
            "matched_site_line": site_line,
            "matched_site_col0": site_col0,
            "clang_line": owner.get("clang_line"),
            "clang_col0": owner.get("clang_col0"),
            "location_origin": location_origin,
            "entry_indices": entry_indices,
            "compiler_path": (
                compilers_sorted[0]["compiler_path"]
                if len(compilers_sorted) == 1
                else None
            ),
            "compiler_id": (
                compilers_sorted[0]["compiler_id"]
                if len(compilers_sorted) == 1
                else None
            ),
            "compilers": compilers_sorted,
            "compile_commands_digest": digest,
            "tree_sitter_members": ts_members,
            "clang_members": clang_members,
            "tree_sitter_member_names": _named_sequence(ts_members),
            "clang_member_names": _named_sequence(clang_members),
            "detail": detail,
            "confidence_boundary": (
                "ordered member names only; type spellings/values/widths are "
                "evidence, not ABI/layout equality"
            ),
        }
        buckets[classification].append(row)

    # Outside-package observations (dedupe by identity).
    seen_outside: Set[str] = set()
    for row in outside_rows:
        key = _canonical_json(
            {
                k: row.get(k)
                for k in ("entity_kind", "name", "source_path", "line", "col0")
            }
        )
        if key in seen_outside:
            continue
        seen_outside.add(key)
        buckets["outside_package_declarations"].append(row)

    def sort_recs(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            rows,
            key=lambda r: (
                str(r.get("entity_kind") or ""),
                str(r.get("source_path") or ""),
                str(r.get("name") or ""),
                str(r.get("tree_sitter_title") or ""),
                int(r.get("matched_site_line") or r.get("line") or 0),
                int(r.get("matched_site_col0") or r.get("col0") or 0),
                _canonical_json(r),
            ),
        )

    for key in _ALL_BUCKETS:
        buckets[key] = sort_recs(buckets[key])

    counts = {key: len(buckets[key]) for key in _ALL_BUCKETS}
    counts["type_declaration_matched_struct_enum"] = len(matched_owners)
    counts["type_declaration_matched_total"] = len(type_report.get("matched") or [])
    counts["shape_owners_classified"] = sum(
        counts[k]
        for k in _ALL_BUCKETS
        if k != "outside_package_declarations"
    )

    compiler_list = list(capture.compilers)
    one = compiler_list[0] if len(compiler_list) == 1 else {}
    translation_units = []
    for ent in capture.entries:
        tu_local = path_is_under(ent.tu_path, package_dir)
        translation_units.append(
            {
                "entry_index": ent.entry_index,
                "file": package_relative_posix(ent.tu_path, package_dir)
                if tu_local
                else None,
                "package_local": tu_local,
                "compiler_path": ent.compiler_path,
                "compiler_id": ent.compiler_id,
            }
        )
    translation_units.sort(
        key=lambda t: (t["entry_index"], str(t.get("file") or ""))
    )

    report: Dict[str, Any] = {
        "mode": MODE,
        "package": package_dir.name,
        "compiler_path": one.get("compiler_path"),
        "compiler_id": one.get("compiler_id"),
        "compiler_version": one.get("compiler_version"),
        "compilers": compiler_list,
        "compile_commands_digest": digest,
        "n_compile_entries": capture.n_compile_entries,
        "translation_units": translation_units,
        "type_declaration_audit_mode": type_report.get("mode"),
        "comparison_contract": {
            "hard_equality": "ordered direct member names only",
            "evidence_only": [
                "qualType",
                "desugaredQualType",
                "enum_value",
                "bit_width",
                "locations",
            ],
            "not_claimed": [
                "size",
                "alignment",
                "offsets",
                "calling_convention",
                "Rust representation compatibility",
                "FFI safety",
                "ABI/layout",
            ],
        },
        "fail_on_mismatch_policy": {
            "exit_1_buckets": list(_FAIL_ON_MISMATCH_BUCKETS),
            "observation_only_buckets": list(_OBSERVATION_ONLY_BUCKETS),
            "note": (
                "outside_package_declarations and unsupported_member_form do "
                "not by themselves cause --fail-on-mismatch to exit 1"
            ),
        },
        "counts": counts,
        "matched_shape": buckets["matched_shape"],
        "tree_sitter_only_members": buckets["tree_sitter_only_members"],
        "clang_only_members": buckets["clang_only_members"],
        "member_order_mismatch": buckets["member_order_mismatch"],
        "duplicate_or_ambiguous_members": buckets["duplicate_or_ambiguous_members"],
        "macro_location_unsupported": buckets["macro_location_unsupported"],
        "unsupported_member_form": buckets["unsupported_member_form"],
        "outside_package_declarations": buckets["outside_package_declarations"],
        "owner_unmatched": buckets["owner_unmatched"],
        "limitations": [
            "Diagnostic only: no BYOG mutation, no index_c flags, no uses_type",
            "Only struct/enum owners already matched by the type-declaration audit",
            "Typedef aliases are not independent shapes",
            "Direct FieldDecl / EnumConstantDecl children only; nested records not flattened",
            "Hard compare = ordered member names; types/values/widths are evidence",
            "Not size/alignment/offset/ABI/layout/FFI/Rust-repr claims",
            "Not multi-config, not C++, not points-to",
        ],
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }

    for key in _ALL_BUCKETS:
        if counts.get(key) != len(report[key]):
            raise ClangTypeShapeAuditError(
                f"internal count/list mismatch for {key}: "
                f"count={counts.get(key)} rows={len(report[key])}"
            )

    normalized = _normalize_report(report)
    try:
        assert_audit_report_matches_capture(
            normalized, capture, context="type-shape audit"
        )
    except ClangAstCaptureError as e:
        raise ClangTypeShapeAuditError(str(e)) from e
    return normalized


def run_clang_type_shape_audit(
    package_dir: Path,
    *,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Run the type-shape audit (one capture, then pure builder)."""
    from graphrag_code.c_clang_ast_capture import (  # type: ignore
        ClangAstCaptureError,
        capture_clang_ast_package,
    )

    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or timeout <= 0
    ):
        raise ClangTypeShapeAuditError(
            f"timeout must be a positive integer, got {timeout!r}"
        )
    try:
        capture = capture_clang_ast_package(package_dir, timeout=timeout)
    except ClangAstCaptureError as e:
        raise ClangTypeShapeAuditError(str(e)) from e
    return build_type_shape_audit_from_capture(capture)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Clang AST JSON type-shape audit: ordered direct member names of "
            "configured matched structs/enums vs tree-sitter (diagnostic only; "
            "not ABI/layout; no BYOG mutation)."
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
            "Exit 1 when internal matchable shape mismatch buckets are nonzero: "
            + ", ".join(_FAIL_ON_MISMATCH_BUCKETS)
            + ". outside_package and unsupported_member_form alone do not fail."
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
        report = run_clang_type_shape_audit(args.package, timeout=args.timeout)
    except ClangTypeShapeAuditError as e:
        print(f"c_clang_type_shape_audit: {e}", file=sys.stderr)
        return 2
    except ClangAstAuditError as e:
        print(f"c_clang_type_shape_audit: {e}", file=sys.stderr)
        return 2

    text = audit_to_json(report)
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        except OSError as e:
            print(
                f"c_clang_type_shape_audit: failed to write output: {e}",
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
                "c_clang_type_shape_audit: --fail-on-mismatch: "
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
