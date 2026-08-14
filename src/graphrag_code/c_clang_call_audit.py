#!/usr/bin/env python
"""Clang AST JSON call-site audit against the tree-sitter-c call graph.

Diagnostic only: compares package-local CallExpr sites from the configured
Clang AST with existing tree-sitter ``calls`` edges (internal) and records
external/indirect calls as observations. Does **not** publish BYOG facts or
add ``index_c`` flags.

Each compile_commands entry is dumped once via the shared AST dump helper.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from graphrag_code.c_clang_ast_audit import (  # type: ignore
    ClangAstAuditError,
    _has_function_body,
    _normalize_path_token,
    resolve_function_location,
)
from graphrag_code.c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    path_is_under,
)
from graphrag_code.c_identities import package_relative_posix  # type: ignore
from graphrag_code.extract_c import build_c_byog  # type: ignore

MODE = "clang_ast_json_call_audit"
CONFIDENCE_BOUNDARY = (
    "Call classifications are configuration/toolchain-derived from the recorded "
    "Clang + compile_commands.json only. Direct internal calls require a "
    "referenced FunctionDecl that resolves unambiguously to a package-local "
    "definition/entity. External and indirect calls remain diagnostic "
    "observations — not points-to analysis, function-pointer target resolution, "
    "interprocedural data flow, full macro expansion, multi-config coverage, or "
    "C++ support. No call facts are published into BYOG."
)

# CallExpr has no top-level loc on Apple clang 17; location is range.begin.
# Source byte offsets are the primary cross-parser identity. Tree-sitter call
# spans remain the strict fallback: 1-based line + 0-based byte column, while
# Clang columns are normalized from 1-based with clang_col0 = clang_col1 - 1.
_TS_CALL_SPAN_RE = re.compile(r"^(?P<line>\d+):(?P<col>\d+)(?:-|$)")


class ClangCallAuditError(CompilerOverlayError):
    """Raised when the call-site audit cannot run honestly."""


# ---------------------------------------------------------------------------
# Pure helpers: callee resolution, location, spans
# ---------------------------------------------------------------------------


def parse_tree_sitter_call_span(span: str) -> Tuple[Optional[int], Optional[int]]:
    """Parse tree-sitter call span ``line:col`` (line 1-based, col 0-based)."""
    m = _TS_CALL_SPAN_RE.match(str(span or "").strip())
    if not m:
        return None, None
    line = int(m.group("line"))
    col = int(m.group("col"))
    if line < 1 or col < 0:
        return None, None
    return line, col


def clang_col_to_zero_based(col1: Optional[int]) -> Optional[int]:
    """Convert Clang 1-based column to tree-sitter 0-based column."""
    if col1 is None:
        return None
    if col1 < 1:
        return None
    return col1 - 1


def source_byte_offset(
    path: Path,
    line: Optional[int],
    col0: Optional[int],
    *,
    cache: Optional[Dict[Path, Tuple[List[bytes], List[int]]]] = None,
) -> Optional[int]:
    """Return a byte offset for a tree-sitter ``line:byte-column`` location."""
    if line is None or col0 is None or line < 1 or col0 < 0:
        return None
    if cache is not None and path in cache:
        rows, starts = cache[path]
    else:
        try:
            rows = path.read_bytes().splitlines(keepends=True)
        except OSError:
            return None
        starts: List[int] = []
        cursor = 0
        for row in rows:
            starts.append(cursor)
            cursor += len(row)
        if cache is not None:
            cache[path] = (rows, starts)
    if line > len(rows):
        return None
    row = rows[line - 1]
    content_len = len(row.rstrip(b"\r\n"))
    if col0 > content_len:
        return None
    return starts[line - 1] + col0


def _unwrap_callee_expr(expr: Any) -> Any:
    """Walk through cast/paren wrappers to the structural callee expression."""
    node = expr
    # Bounded unwrap for ImplicitCastExpr / ParenExpr / CStyleCastExpr / etc.
    for _ in range(12):
        if not isinstance(node, dict):
            return node
        kind = node.get("kind")
        if kind in {
            "ImplicitCastExpr",
            "CStyleCastExpr",
            "CXXStaticCastExpr",
            "CXXFunctionalCastExpr",
            "ParenExpr",
            "UnaryOperator",  # e.g. &* — dig one level
        }:
            inner = node.get("inner") or []
            if not inner:
                return node
            # UnaryOperator / cast: first child is the operand
            node = inner[0]
            continue
        return node
    return node


@dataclass
class CalleeResolution:
    kind: str  # direct_function | indirect | unresolved
    ref_kind: Optional[str] = None
    ref_name: Optional[str] = None
    ref_id: Optional[str] = None
    ref_type: Optional[str] = None
    member_name: Optional[str] = None
    reason: str = ""


def resolve_callee_expression(callee_expr: Any) -> CalleeResolution:
    """Classify the CallExpr callee subtree only (never argument children)."""
    node = _unwrap_callee_expr(callee_expr)
    if not isinstance(node, dict):
        return CalleeResolution("unresolved", reason="callee expression is not a node")

    kind = node.get("kind")
    if kind == "DeclRefExpr":
        rd = node.get("referencedDecl") if isinstance(node.get("referencedDecl"), dict) else {}
        rd_kind = rd.get("kind")
        rd_name = rd.get("name")
        rd_id = rd.get("id")
        rd_type = (
            (rd.get("type") or {}).get("qualType")
            if isinstance(rd.get("type"), dict)
            else None
        )
        if rd_kind == "FunctionDecl":
            return CalleeResolution(
                "direct_function",
                ref_kind=rd_kind,
                ref_name=rd_name,
                ref_id=rd_id,
                ref_type=rd_type,
                reason="DeclRefExpr -> FunctionDecl",
            )
        if rd_kind in {"ParmVarDecl", "VarDecl"}:
            return CalleeResolution(
                "indirect",
                ref_kind=rd_kind,
                ref_name=rd_name,
                ref_id=rd_id,
                ref_type=rd_type,
                reason=f"DeclRefExpr -> {rd_kind} (function pointer value)",
            )
        return CalleeResolution(
            "indirect",
            ref_kind=rd_kind,
            ref_name=rd_name,
            ref_id=rd_id,
            ref_type=rd_type,
            reason=f"DeclRefExpr -> {rd_kind or 'unknown'}",
        )

    if kind == "MemberExpr":
        # Field / member function-pointer; do not guess target from type text.
        mtype = (
            (node.get("type") or {}).get("qualType")
            if isinstance(node.get("type"), dict)
            else None
        )
        return CalleeResolution(
            "indirect",
            ref_kind="FieldDecl",
            ref_name=node.get("name"),
            ref_id=node.get("referencedMemberDecl"),
            ref_type=mtype,
            member_name=node.get("name"),
            reason="MemberExpr field/function-pointer member",
        )

    return CalleeResolution(
        "unresolved",
        ref_kind=kind,
        reason=f"unsupported callee expression kind {kind!r}",
    )


def call_site_location(
    call_node: Dict[str, Any],
    *,
    last_file: Optional[str],
) -> Tuple[
    Optional[str],
    Optional[int],
    Optional[int],
    Optional[int],
    Optional[int],
    str,
    Optional[str],
    Optional[str],
]:
    """Return file/line/columns/byte-offset and macro paths for a CallExpr.

    Apple clang 17 CallExpr dumps use ``range.begin`` (no top-level ``loc``).
    The byte ``offset`` is retained because Clang frequently omits ``line``.
    """
    rng = call_node.get("range") if isinstance(call_node.get("range"), dict) else {}
    begin = rng.get("begin") if isinstance(rng, dict) else None
    # Also check range.end for a line if begin lacks one (probed dumps).
    end = rng.get("end") if isinstance(rng, dict) else None
    raw_loc = begin if isinstance(begin, dict) else call_node.get("loc")
    primary, spelling, expansion = resolve_function_location(
        raw_loc,
        last_file=last_file,
        range_begin=begin if isinstance(begin, dict) else None,
    )
    selected_file = primary.file
    selected_line = primary.line
    selected_col1 = primary.col
    selected_origin = primary.origin
    offset = None
    if isinstance(raw_loc, dict):
        chosen = raw_loc
        if "spellingLoc" in raw_loc or "expansionLoc" in raw_loc:
            exp = raw_loc.get("expansionLoc")
            spell = raw_loc.get("spellingLoc")
            if isinstance(exp, dict) and any(
                exp.get(k) is not None for k in ("offset", "line", "col")
            ):
                chosen = exp
                selected_file = expansion.file
                selected_line = exp.get("line")
                selected_col1 = exp.get("col")
                selected_origin = "expansion"
            elif isinstance(spell, dict):
                chosen = spell
                selected_file = spelling.file
                selected_line = spell.get("line")
                selected_col1 = spell.get("col")
                selected_origin = "spelling"
        if chosen.get("offset") is not None:
            try:
                offset = int(chosen["offset"])
            except (TypeError, ValueError):
                offset = None
    line = selected_line
    if line is None and isinstance(end, dict) and end.get("line") is not None:
        line = end.get("line")
    # Do NOT inherit a line for plain CallExpr rows that omit it: stale lines
    # from prior macro expansions (e.g. assert.h) re-parent later calls.
    # Macro multi-file detection uses spelling/expansion files as recorded.
    return (
        selected_file,
        int(line) if line is not None else None,
        clang_col_to_zero_based(selected_col1),
        selected_col1,
        offset,
        selected_origin,
        spelling.file,
        expansion.file,
    )


# ---------------------------------------------------------------------------
# AST indexing: FunctionDecl IDs, redeclarations, package definitions
# ---------------------------------------------------------------------------


@dataclass
class FnDeclInfo:
    decl_id: str
    name: str
    has_body: bool
    previous_decl: Optional[str]
    source_path: Optional[str]  # package-relative when local
    is_package_local: bool
    line: Optional[int]
    col: Optional[int]
    qual_type: Optional[str]
    is_implicit: bool


def index_function_decls(
    root: Any,
    *,
    package_dir: Path,
    cwd: Path,
) -> Tuple[Dict[str, FnDeclInfo], Dict[str, List[str]], Dict[Tuple[str, str], List[str]]]:
    """Build id->info, previousDecl reverse edges, and (path,name)->def ids."""
    package_dir = package_dir.resolve()
    cwd = cwd.resolve()
    by_id: Dict[str, FnDeclInfo] = {}
    prev_to_ids: Dict[str, List[str]] = {}
    path_name_to_def_ids: Dict[Tuple[str, str], List[str]] = {}
    last_file: Optional[str] = None

    def record_fn(node: Dict[str, Any], *, update_context: bool) -> None:
        nonlocal last_file
        loc = node.get("loc") if isinstance(node.get("loc"), dict) else None
        rng = node.get("range") if isinstance(node.get("range"), dict) else None
        range_begin = rng.get("begin") if isinstance(rng, dict) else None
        primary, _, _ = resolve_function_location(
            loc, last_file=last_file, range_begin=range_begin
        )
        if update_context and primary.file:
            last_file = primary.file
        rel, _, is_local = _normalize_path_token(
            primary.file, cwd=cwd, package_dir=package_dir
        )
        decl_id = str(node.get("id"))
        prev = node.get("previousDecl")
        prev_s = str(prev) if prev else None
        info = FnDeclInfo(
            decl_id=decl_id,
            name=str(node.get("name") or ""),
            has_body=_has_function_body(node),
            previous_decl=prev_s,
            source_path=rel if is_local else None,
            is_package_local=bool(is_local and rel),
            line=primary.line,
            col=primary.col,
            qual_type=(
                (node.get("type") or {}).get("qualType")
                if isinstance(node.get("type"), dict)
                else None
            ),
            is_implicit=bool(node.get("isImplicit")),
        )
        by_id[decl_id] = info
        if prev_s:
            prev_to_ids.setdefault(prev_s, []).append(decl_id)
        if info.has_body and info.is_package_local and info.source_path and info.name:
            path_name_to_def_ids.setdefault(
                (info.source_path, info.name), []
            ).append(decl_id)

    def walk(node: Any) -> None:
        nonlocal last_file
        if not isinstance(node, dict):
            return
        # Implicit builtins: index FunctionDecl without polluting file context.
        if node.get("isImplicit"):
            if node.get("kind") == "FunctionDecl" and node.get("id"):
                record_fn(node, update_context=False)
            return

        loc = node.get("loc") if isinstance(node.get("loc"), dict) else None
        if isinstance(loc, dict) and loc.get("file"):
            last_file = str(loc["file"])
        elif isinstance(loc, dict):
            for key in ("expansionLoc", "spellingLoc"):
                nested = loc.get(key)
                if isinstance(nested, dict) and nested.get("file"):
                    last_file = str(nested["file"])
                    break

        if node.get("kind") == "FunctionDecl" and node.get("id"):
            record_fn(node, update_context=True)

        for child in node.get("inner") or []:
            walk(child)

    walk(root)
    return by_id, prev_to_ids, path_name_to_def_ids


def resolve_function_decl_to_package_def(
    decl_id: Optional[str],
    *,
    by_id: Dict[str, FnDeclInfo],
    prev_to_ids: Dict[str, List[str]],
    path_name_to_def_ids: Dict[Tuple[str, str], List[str]],
) -> Tuple[Optional[FnDeclInfo], str]:
    """Resolve a referenced FunctionDecl id to a package-local definition.

    Returns (def_info_or_None, reason).
    """
    if not decl_id or decl_id not in by_id:
        return None, "referenced FunctionDecl id not in AST index"

    # Collect the redeclaration chain (walk previousDecl backward + forward).
    chain_ids: Set[str] = set()
    stack = [decl_id]
    while stack:
        cur = stack.pop()
        if cur in chain_ids or cur not in by_id:
            continue
        chain_ids.add(cur)
        info = by_id[cur]
        if info.previous_decl:
            stack.append(info.previous_decl)
        for later in prev_to_ids.get(cur, []):
            stack.append(later)

    body_locals = [
        by_id[i]
        for i in chain_ids
        if by_id[i].has_body and by_id[i].is_package_local and not by_id[i].is_implicit
    ]
    if len(body_locals) == 1:
        return body_locals[0], "redeclaration_chain_unique_package_definition"
    if len(body_locals) > 1:
        return None, "redeclaration_chain_multiple_package_definitions"

    # No body in chain: only a package-local prototype may fall back by name.
    # An external/system declaration with the same name as an unrelated local
    # static definition must remain external.
    referenced = by_id[decl_id]
    if not referenced.is_package_local:
        return None, "no_package_local_definition"
    name = referenced.name
    if not name:
        return None, "referenced FunctionDecl has empty name"
    same_path_defs = [
        by_id[i]
        for (path, candidate_name), ids in path_name_to_def_ids.items()
        if path == referenced.source_path and candidate_name == name
        for i in ids
    ]
    if len(same_path_defs) == 1:
        return same_path_defs[0], "unique_same_file_package_definition_by_name"
    if len(same_path_defs) > 1:
        return None, "multiple_same_file_package_definitions_same_name"
    name_defs = [
        by_id[i]
        for key, ids in path_name_to_def_ids.items()
        if key[1] == name
        for i in ids
    ]
    # Unique across entire package by name?
    if len(name_defs) == 1:
        return name_defs[0], "unique_package_definition_by_name"
    if len(name_defs) > 1:
        return None, "multiple_package_definitions_same_name"
    return None, "no_package_local_definition"


# ---------------------------------------------------------------------------
# Call extraction from one AST
# ---------------------------------------------------------------------------


@dataclass
class RawClangCall:
    caller_name: str
    caller_path: str
    caller_title: Optional[str]
    line: Optional[int]
    col0: Optional[int]
    clang_col1: Optional[int]
    location_origin: str
    classification: str
    byte_offset: Optional[int] = None
    observation_index: int = 0
    target_name: Optional[str] = None
    target_path: Optional[str] = None
    target_title: Optional[str] = None
    ref_id: Optional[str] = None
    ref_kind: Optional[str] = None
    ref_type: Optional[str] = None
    member_name: Optional[str] = None
    resolve_reason: str = ""
    entry_index: int = 0
    compiler_path: Optional[str] = None
    compiler_id: Optional[str] = None
    compile_commands_digest: Optional[str] = None
    spelling_path: Optional[str] = None
    expansion_path: Optional[str] = None


def collect_calls_from_ast(
    root: Any,
    *,
    package_dir: Path,
    cwd: Path,
    entry_index: int,
    compiler_path: str,
    compiler_id: Optional[str],
    compile_commands_digest: str,
    title_by_path_name: Dict[Tuple[str, str], List[str]],
) -> Tuple[List[RawClangCall], Set[str]]:
    """Extract package-local call sites; return (calls, in_scope_paths)."""
    package_dir = package_dir.resolve()
    cwd = cwd.resolve()
    by_id, prev_to_ids, path_name_defs = index_function_decls(
        root, package_dir=package_dir, cwd=cwd
    )
    in_scope: Set[str] = set()
    for (path, _name), _ids in path_name_defs.items():
        in_scope.add(path)

    calls: List[RawClangCall] = []
    last_file: Optional[str] = None

    def walk(node: Any, caller: Optional[Tuple[str, str, Optional[str]]] = None) -> None:
        """caller = (name, path, title) when inside a package-local definition."""
        nonlocal last_file
        if not isinstance(node, dict):
            return

        if node.get("isImplicit"):
            return

        loc = node.get("loc") if isinstance(node.get("loc"), dict) else None
        if isinstance(loc, dict) and loc.get("file"):
            last_file = str(loc["file"])
        elif isinstance(loc, dict):
            for key in ("expansionLoc", "spellingLoc"):
                nested = loc.get(key)
                if isinstance(nested, dict) and nested.get("file"):
                    last_file = str(nested["file"])
                    break

        # Enter package-local function definitions as call parents.
        if (
            node.get("kind") == "FunctionDecl"
            and node.get("name")
            and _has_function_body(node)
        ):
            rng = node.get("range") if isinstance(node.get("range"), dict) else None
            range_begin = rng.get("begin") if isinstance(rng, dict) else None
            primary, _, _ = resolve_function_location(
                loc, last_file=last_file, range_begin=range_begin
            )
            if primary.file:
                last_file = primary.file
            rel, _, is_local = _normalize_path_token(
                primary.file, cwd=cwd, package_dir=package_dir
            )
            if is_local and rel:
                in_scope.add(rel)
                name = str(node.get("name"))
                titles = title_by_path_name.get((rel, name), [])
                title = titles[0] if len(titles) == 1 else None
                # Walk children with this caller; do not use non-local as caller.
                for child in node.get("inner") or []:
                    walk(child, caller=(name, rel, title))
                return
            # Non-package definition: still walk for context but no caller.
            for child in node.get("inner") or []:
                walk(child, caller=None)
            return

        if node.get("kind") == "CallExpr" and caller is not None:
            call_start = len(calls)
            c_name, c_path, c_title = caller
            file_tok, line, col0, col1, byte_offset, origin, spell_f, exp_f = call_site_location(
                node, last_file=last_file
            )
            spell_rel, _, spell_local = _normalize_path_token(
                spell_f, cwd=cwd, package_dir=package_dir
            )
            exp_rel, _, exp_local = _normalize_path_token(
                exp_f, cwd=cwd, package_dir=package_dir
            )
            site_rel, _, site_local = _normalize_path_token(
                file_tok, cwd=cwd, package_dir=package_dir
            )
            # Macro multi-file disagreement on package paths.
            if c_title is None:
                calls.append(
                    RawClangCall(
                        caller_name=c_name,
                        caller_path=c_path,
                        caller_title=None,
                        line=line,
                        col0=col0,
                        clang_col1=col1,
                        location_origin=origin,
                        classification="ambiguous",
                        resolve_reason="no unique tree-sitter caller entity",
                        entry_index=entry_index,
                        compiler_path=compiler_path,
                        compiler_id=compiler_id,
                        compile_commands_digest=compile_commands_digest,
                    )
                )
            elif byte_offset is None and (line is None or col0 is None):
                calls.append(
                    RawClangCall(
                        caller_name=c_name,
                        caller_path=c_path,
                        caller_title=c_title,
                        line=line,
                        col0=col0,
                        clang_col1=col1,
                        location_origin=origin,
                        classification="ambiguous",
                        resolve_reason=(
                            "CallExpr has neither byte offset nor complete line/column location"
                        ),
                        entry_index=entry_index,
                        compiler_path=compiler_path,
                        compiler_id=compiler_id,
                        compile_commands_digest=compile_commands_digest,
                    )
                )
            elif site_local and site_rel and site_rel != c_path:
                calls.append(
                    RawClangCall(
                        caller_name=c_name,
                        caller_path=c_path,
                        caller_title=c_title,
                        line=line,
                        col0=col0,
                        clang_col1=col1,
                        location_origin=origin,
                        classification="macro_location_unsupported",
                        resolve_reason=(
                            f"call location path {site_rel} differs from caller path {c_path}"
                        ),
                        spelling_path=spell_rel,
                        expansion_path=exp_rel,
                        entry_index=entry_index,
                        compiler_path=compiler_path,
                        compiler_id=compiler_id,
                        compile_commands_digest=compile_commands_digest,
                    )
                )
            elif (
                spell_local
                and exp_local
                and spell_rel
                and exp_rel
                and spell_rel != exp_rel
            ):
                calls.append(
                    RawClangCall(
                        caller_name=c_name,
                        caller_path=c_path,
                        caller_title=c_title,
                        line=line,
                        col0=col0,
                        clang_col1=col1,
                        location_origin="spelling+expansion",
                        classification="macro_location_unsupported",
                        resolve_reason="package spelling/expansion paths disagree",
                        spelling_path=spell_rel,
                        expansion_path=exp_rel,
                        entry_index=entry_index,
                        compiler_path=compiler_path,
                        compiler_id=compiler_id,
                        compile_commands_digest=compile_commands_digest,
                    )
                )
            else:
                inner = node.get("inner") or []
                if not inner:
                    calls.append(
                        RawClangCall(
                            caller_name=c_name,
                            caller_path=c_path,
                            caller_title=c_title,
                            line=line,
                            col0=col0,
                            clang_col1=col1,
                            location_origin=origin,
                            classification="ambiguous",
                            resolve_reason="CallExpr has empty inner (no callee)",
                            entry_index=entry_index,
                            compiler_path=compiler_path,
                            compiler_id=compiler_id,
                            compile_commands_digest=compile_commands_digest,
                        )
                    )
                else:
                    # Callee is the first child only — never scan arguments.
                    cal = resolve_callee_expression(inner[0])
                    if cal.kind == "indirect":
                        calls.append(
                            RawClangCall(
                                caller_name=c_name,
                                caller_path=c_path,
                                caller_title=c_title,
                                line=line,
                                col0=col0,
                                clang_col1=col1,
                                location_origin=origin,
                                classification="indirect",
                                target_name=cal.ref_name,
                                ref_id=cal.ref_id,
                                ref_kind=cal.ref_kind,
                                ref_type=cal.ref_type,
                                member_name=cal.member_name,
                                resolve_reason=cal.reason,
                                entry_index=entry_index,
                                compiler_path=compiler_path,
                                compiler_id=compiler_id,
                                compile_commands_digest=compile_commands_digest,
                            )
                        )
                    elif cal.kind == "direct_function":
                        def_info, reason = resolve_function_decl_to_package_def(
                            cal.ref_id,
                            by_id=by_id,
                            prev_to_ids=prev_to_ids,
                            path_name_to_def_ids=path_name_defs,
                        )
                        if def_info is None:
                            # External or unresolvable package target.
                            if reason != "no_package_local_definition":
                                calls.append(
                                    RawClangCall(
                                        caller_name=c_name,
                                        caller_path=c_path,
                                        caller_title=c_title,
                                        line=line,
                                        col0=col0,
                                        clang_col1=col1,
                                        location_origin=origin,
                                        classification="ambiguous",
                                        target_name=cal.ref_name,
                                        ref_id=cal.ref_id,
                                        ref_kind=cal.ref_kind,
                                        ref_type=cal.ref_type,
                                        resolve_reason=reason,
                                        entry_index=entry_index,
                                        compiler_path=compiler_path,
                                        compiler_id=compiler_id,
                                        compile_commands_digest=compile_commands_digest,
                                    )
                                )
                            else:
                                calls.append(
                                    RawClangCall(
                                        caller_name=c_name,
                                        caller_path=c_path,
                                        caller_title=c_title,
                                        line=line,
                                        col0=col0,
                                        clang_col1=col1,
                                        location_origin=origin,
                                        classification="external_direct",
                                        target_name=cal.ref_name,
                                        ref_id=cal.ref_id,
                                        ref_kind=cal.ref_kind,
                                        ref_type=cal.ref_type,
                                        resolve_reason=reason,
                                        entry_index=entry_index,
                                        compiler_path=compiler_path,
                                        compiler_id=compiler_id,
                                        compile_commands_digest=compile_commands_digest,
                                    )
                                )
                        else:
                            titles = title_by_path_name.get(
                                (def_info.source_path or "", def_info.name), []
                            )
                            if len(titles) == 1:
                                calls.append(
                                    RawClangCall(
                                        caller_name=c_name,
                                        caller_path=c_path,
                                        caller_title=c_title,
                                        line=line,
                                        col0=col0,
                                        clang_col1=col1,
                                        location_origin=origin,
                                        classification="internal_direct",
                                        target_name=def_info.name,
                                        target_path=def_info.source_path,
                                        target_title=titles[0],
                                        ref_id=cal.ref_id,
                                        ref_kind=cal.ref_kind,
                                        ref_type=cal.ref_type or def_info.qual_type,
                                        resolve_reason=reason,
                                        entry_index=entry_index,
                                        compiler_path=compiler_path,
                                        compiler_id=compiler_id,
                                        compile_commands_digest=compile_commands_digest,
                                    )
                                )
                            elif len(titles) > 1:
                                calls.append(
                                    RawClangCall(
                                        caller_name=c_name,
                                        caller_path=c_path,
                                        caller_title=c_title,
                                        line=line,
                                        col0=col0,
                                        clang_col1=col1,
                                        location_origin=origin,
                                        classification="ambiguous",
                                        target_name=def_info.name,
                                        target_path=def_info.source_path,
                                        ref_id=cal.ref_id,
                                        ref_kind=cal.ref_kind,
                                        ref_type=cal.ref_type,
                                        resolve_reason=(
                                            "multiple tree-sitter entities for "
                                            f"path+name {def_info.source_path}:{def_info.name}"
                                        ),
                                        entry_index=entry_index,
                                        compiler_path=compiler_path,
                                        compiler_id=compiler_id,
                                        compile_commands_digest=compile_commands_digest,
                                    )
                                )
                            else:
                                # Package def exists in AST but not in tree-sitter.
                                calls.append(
                                    RawClangCall(
                                        caller_name=c_name,
                                        caller_path=c_path,
                                        caller_title=c_title,
                                        line=line,
                                        col0=col0,
                                        clang_col1=col1,
                                        location_origin=origin,
                                        classification="internal_direct",
                                        target_name=def_info.name,
                                        target_path=def_info.source_path,
                                        target_title=None,
                                        ref_id=cal.ref_id,
                                        ref_kind=cal.ref_kind,
                                        ref_type=cal.ref_type or def_info.qual_type,
                                        resolve_reason=(
                                            reason
                                            + "; no tree-sitter entity for package definition"
                                        ),
                                        entry_index=entry_index,
                                        compiler_path=compiler_path,
                                        compiler_id=compiler_id,
                                        compile_commands_digest=compile_commands_digest,
                                    )
                                )
                    else:
                        calls.append(
                            RawClangCall(
                                caller_name=c_name,
                                caller_path=c_path,
                                caller_title=c_title,
                                line=line,
                                col0=col0,
                                clang_col1=col1,
                                location_origin=origin,
                                classification="ambiguous",
                                ref_kind=cal.ref_kind,
                                resolve_reason=cal.reason or "unresolved callee",
                                entry_index=entry_index,
                                compiler_path=compiler_path,
                                compiler_id=compiler_id,
                                compile_commands_digest=compile_commands_digest,
                            )
                        )

            for observed in calls[call_start:]:
                observed.byte_offset = byte_offset

        for child in node.get("inner") or []:
            walk(child, caller)

    walk(root, None)
    for observation_index, call in enumerate(calls):
        call.observation_index = observation_index
    return calls, in_scope


# ---------------------------------------------------------------------------
# Dedup + comparison with tree-sitter
# ---------------------------------------------------------------------------


def _call_identity_key(c: RawClangCall) -> Tuple:
    """Stable semantic identity for a call observation.

    Deliberately omits AST node ``ref_id`` — Clang assigns fresh pointer-like
    ids each dump, so including them would false-conflict identical calls
    across duplicate compile entries.
    """
    return (
        c.classification,
        c.target_title or "",
        c.target_name or "",
        c.target_path or "",
        c.ref_kind or "",
        c.member_name or "",
        c.ref_type or "",
    )


def _physical_site_key(c: RawClangCall) -> Tuple:
    prefix = (c.caller_path, c.caller_name, c.caller_title or "")
    if c.byte_offset is not None:
        return (*prefix, "byte_offset", c.byte_offset, -1)
    if c.line is not None and c.col0 is not None:
        return (*prefix, "line_col", c.line, c.col0)
    # An incomplete location cannot safely deduplicate across entries or even
    # with another call in the same AST.
    return (*prefix, "unlocated", c.entry_index, c.observation_index)


def _observation(c: RawClangCall) -> Dict[str, Any]:
    return {
        "classification": c.classification,
        "target_title": c.target_title,
        "target_name": c.target_name,
        "target_path": c.target_path,
        "ref_kind": c.ref_kind,
        "ref_type": c.ref_type,
        "member_name": c.member_name,
        "resolve_reason": c.resolve_reason,
        "location_origin": c.location_origin,
        "spelling_path": c.spelling_path,
        "expansion_path": c.expansion_path,
        "entry_index": c.entry_index,
        "compiler_path": c.compiler_path,
        "compiler_id": c.compiler_id,
        "compile_commands_digest": c.compile_commands_digest,
    }


def merge_clang_calls(calls: Sequence[RawClangCall]) -> List[Dict[str, Any]]:
    """Merge duplicate-entry observations without collapsing nested macro calls."""
    # Group by physical call site first.
    by_site: Dict[Tuple, List[RawClangCall]] = {}
    for c in calls:
        by_site.setdefault(_physical_site_key(c), []).append(c)

    def merged_row(items: Sequence[RawClangCall]) -> Dict[str, Any]:
        base = items[0]
        entry_indices = sorted({c.entry_index for c in items})
        observations = [
            _observation(c)
            for c in sorted(
                items,
                key=lambda x: (
                    x.entry_index,
                    x.compiler_path or "",
                    x.resolve_reason,
                ),
            )
        ]
        compilers = sorted(
            {
                (
                    c.compiler_path or "",
                    c.compiler_id or "",
                    c.compile_commands_digest or "",
                )
                for c in items
            }
        )
        reasons = sorted({c.resolve_reason for c in items if c.resolve_reason})
        return {
            "classification": base.classification,
            "caller_name": base.caller_name,
            "caller_path": base.caller_path,
            "caller_title": base.caller_title,
            "line": base.line,
            "col0": base.col0,
            "clang_col1": base.clang_col1,
            "byte_offset": base.byte_offset,
            "location_origin": base.location_origin,
            "target_name": base.target_name,
            "target_path": base.target_path,
            "target_title": base.target_title,
            # Omit pointer-like AST node ids (non-deterministic across dumps).
            "ref_kind": base.ref_kind,
            "ref_type": base.ref_type,
            "member_name": base.member_name,
            "resolve_reason": reasons[0] if len(reasons) == 1 else None,
            "resolve_reasons": reasons,
            "spelling_path": base.spelling_path,
            "expansion_path": base.expansion_path,
            "entry_indices": entry_indices,
            "observations": observations,
            "compilers": [
                {
                    "compiler_path": p or None,
                    "compiler_id": i or None,
                    "compile_commands_digest": d or None,
                }
                for p, i, d in compilers
            ],
            "compiler_path": (compilers[0][0] or None) if len(compilers) == 1 else None,
            "compiler_id": (compilers[0][1] or None) if len(compilers) == 1 else None,
            "compile_commands_digest": (
                (compilers[0][2] or None) if len(compilers) == 1 else None
            ),
        }

    def conflict_row(group: Sequence[RawClangCall]) -> Dict[str, Any]:
        compilers = sorted(
            {
                (
                    c.compiler_path or "",
                    c.compiler_id or "",
                    c.compile_commands_digest or "",
                )
                for c in group
            }
        )
        return {
            "classification": "ambiguous",
            "caller_name": group[0].caller_name,
            "caller_path": group[0].caller_path,
            "caller_title": group[0].caller_title,
            "line": group[0].line,
            "col0": group[0].col0,
            "clang_col1": group[0].clang_col1,
            "byte_offset": group[0].byte_offset,
            "reason": "conflicting compile-entry call multisets for source site",
            "entry_indices": sorted({c.entry_index for c in group}),
            "observations": [
                _observation(c)
                for c in sorted(
                    group,
                    key=lambda x: (
                        x.entry_index,
                        _call_identity_key(x),
                        x.observation_index,
                    ),
                )
            ],
            "compilers": [
                {
                    "compiler_path": p or None,
                    "compiler_id": i or None,
                    "compile_commands_digest": d or None,
                }
                for p, i, d in compilers
            ],
        }

    out: List[Dict[str, Any]] = []
    for _site, group in sorted(by_site.items()):
        entry_indices = sorted({c.entry_index for c in group})
        per_entry: Dict[int, Dict[Tuple, List[RawClangCall]]] = {}
        for c in group:
            per_entry.setdefault(c.entry_index, {}).setdefault(
                _call_identity_key(c), []
            ).append(c)

        signatures = [
            sorted((key, len(items)) for key, items in per_entry[index].items())
            for index in entry_indices
        ]
        if len(signatures) > 1 and any(s != signatures[0] for s in signatures[1:]):
            out.append(conflict_row(group))
            continue

        # A macro expansion may produce several nested CallExpr nodes with the
        # same source offset. They are a multiset, not conflicting identities.
        for identity, multiplicity in signatures[0]:
            for occurrence in range(multiplicity):
                occurrence_items = [
                    sorted(
                        per_entry[index][identity],
                        key=lambda c: (c.observation_index, c.resolve_reason),
                    )[occurrence]
                    for index in entry_indices
                ]
                out.append(merged_row(occurrence_items))

    out.sort(
        key=lambda r: (
            str(r.get("caller_path") or ""),
            str(r.get("caller_title") or r.get("caller_name") or ""),
            int(r.get("line") or 0),
            int(r.get("col0") if r.get("col0") is not None else -1),
            int(r.get("byte_offset") if r.get("byte_offset") is not None else -1),
            str(r.get("classification") or ""),
            str(r.get("target_title") or r.get("target_name") or ""),
            json.dumps(r, sort_keys=True),
        )
    )
    return out


@dataclass(frozen=True)
class TSCallEdge:
    caller_title: str
    target_title: str
    source_path: str
    line: Optional[int]
    col0: Optional[int]
    span: str
    byte_offset: Optional[int] = None


def collect_tree_sitter_calls(
    package_dir: Path,
) -> Tuple[List[TSCallEdge], Dict[Tuple[str, str], List[str]], Dict[str, str], Any]:
    """Return TS call edges, (path,name)->titles, title->path, and full byog data."""
    package_dir = package_dir.resolve()
    data = build_c_byog(package_dir)
    title_to_path: Dict[str, str] = {}
    path_name_to_titles: Dict[Tuple[str, str], List[str]] = {}
    for e in data.get("entities") or []:
        if str(e.get("type")) != "function":
            continue
        title = str(e.get("title") or "")
        raw_name = e.get("symbol_name")
        name = (
            raw_name.strip()
            if isinstance(raw_name, str) and raw_name.strip()
            else title.rsplit(":", 1)[-1] if title else ""
        )
        sf = str(e.get("source_file") or "")
        p = Path(sf)
        if not p.is_absolute():
            p = (package_dir / p).resolve()
        else:
            p = p.resolve()
        rel = package_relative_posix(p, package_dir)
        title_to_path[title] = rel
        path_name_to_titles.setdefault((rel, name), []).append(title)

    for key in path_name_to_titles:
        path_name_to_titles[key] = sorted(set(path_name_to_titles[key]))

    edges: List[TSCallEdge] = []
    offset_cache: Dict[Path, Tuple[List[bytes], List[int]]] = {}
    for r in data.get("relationships") or []:
        if str(r.get("type")) != "calls":
            continue
        caller = str(r.get("source") or "")
        target = str(r.get("target") or "")
        sf = str(r.get("source_file") or "")
        if sf:
            p = Path(sf)
            if not p.is_absolute():
                p = (package_dir / p).resolve()
            else:
                p = p.resolve()
            rel = package_relative_posix(p, package_dir)
        else:
            rel = title_to_path.get(caller, "")
        line, col0 = parse_tree_sitter_call_span(str(r.get("span") or ""))
        edges.append(
            TSCallEdge(
                caller_title=caller,
                target_title=target,
                source_path=rel,
                line=line,
                col0=col0,
                span=str(r.get("span") or ""),
                byte_offset=source_byte_offset(
                    package_dir / rel, line, col0, cache=offset_cache
                )
                if rel
                else None,
            )
        )
    edges.sort(
        key=lambda e: (
            e.source_path,
            e.caller_title,
            e.line or 0,
            e.col0 if e.col0 is not None else -1,
            e.byte_offset if e.byte_offset is not None else -1,
            e.target_title,
            e.span,
        )
    )
    return edges, path_name_to_titles, title_to_path, data


def compare_calls(
    *,
    clang_rows: Sequence[Dict[str, Any]],
    ts_edges: Sequence[TSCallEdge],
    in_scope_paths: Set[str],
) -> Dict[str, Any]:
    """Build mutually exclusive comparison buckets."""
    matched_internal: List[Dict[str, Any]] = []
    clang_only_internal: List[Dict[str, Any]] = []
    external_direct: List[Dict[str, Any]] = []
    indirect: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    macro_unsupported: List[Dict[str, Any]] = []

    internal_rows: List[Dict[str, Any]] = []
    noninternal_rows: List[Dict[str, Any]] = []

    for original in clang_rows:
        row = dict(original)
        cls = row.get("classification")
        if cls == "internal_direct":
            if row.get("target_title") and row.get("caller_title"):
                internal_rows.append(row)
            else:
                clang_only_internal.append(row)
        elif cls == "external_direct":
            external_direct.append(row)
            noninternal_rows.append(row)
        elif cls == "indirect":
            indirect.append(row)
            noninternal_rows.append(row)
        elif cls == "ambiguous":
            ambiguous.append(row)
            noninternal_rows.append(row)
        elif cls == "macro_location_unsupported":
            macro_unsupported.append(row)
            noninternal_rows.append(row)
        else:
            row["classification"] = "ambiguous"
            row["reason"] = f"unknown Clang call classification {cls!r}"
            ambiguous.append(row)
            noninternal_rows.append(row)

    ts_only: List[Dict[str, Any]] = []
    out_of_scope: List[Dict[str, Any]] = []
    used_internal: Set[int] = set()
    used_noninternal: Set[int] = set()
    covered_by_noninternal = 0

    def _same_call(
        row: Dict[str, Any], edge: TSCallEdge, *, include_target: bool
    ) -> Optional[str]:
        caller = row.get("caller_title")
        if not caller or str(caller) != edge.caller_title:
            return None
        if str(row.get("caller_path") or "") != edge.source_path:
            return None
        if include_target and str(row.get("target_title") or "") != edge.target_title:
            return None
        row_offset = row.get("byte_offset")
        if row_offset is not None and edge.byte_offset is not None:
            return "exact_byte_offset" if int(row_offset) == edge.byte_offset else None
        if (
            row.get("line") is not None
            and row.get("col0") is not None
            and edge.line is not None
            and edge.col0 is not None
            and int(row["line"]) == edge.line
            and int(row["col0"]) == edge.col0
        ):
            return "exact_line_col_fallback"
        return None

    def _record_match(edge: TSCallEdge, crow: Dict[str, Any], how: str) -> None:
        matched_internal.append(
            {
                "caller_title": edge.caller_title,
                "target_title": edge.target_title,
                "source_path": edge.source_path,
                "line": edge.line,
                "col0": edge.col0,
                "byte_offset": edge.byte_offset,
                "tree_sitter_span": edge.span,
                "clang_line": crow.get("line"),
                "clang_col1": crow.get("clang_col1"),
                "clang_byte_offset": crow.get("byte_offset"),
                "match_basis": how,
                "clang_entry_indices": list(crow.get("entry_indices") or []),
                "clang_resolve_reason": crow.get("resolve_reason"),
                "ref_kind": crow.get("ref_kind"),
                "ref_type": crow.get("ref_type"),
                "compiler_path": crow.get("compiler_path"),
                "compiler_id": crow.get("compiler_id"),
                "compile_commands_digest": crow.get("compile_commands_digest"),
                "clang_observations": list(crow.get("observations") or []),
                "clang_compilers": list(crow.get("compilers") or []),
            }
        )

    for edge in ts_edges:
        match = next(
            (
                (idx, basis)
                for idx, row in enumerate(internal_rows)
                if idx not in used_internal
                for basis in [_same_call(row, edge, include_target=True)]
                if basis is not None
            ),
            None,
        )
        if match is not None:
            idx, basis = match
            used_internal.add(idx)
            _record_match(edge, internal_rows[idx], basis)
            continue

        covered_candidates = [
            (idx, basis)
            for idx, row in enumerate(noninternal_rows)
            if idx not in used_noninternal
            for basis in [_same_call(row, edge, include_target=False)]
            if basis is not None
        ]
        # Several macro-expanded Clang calls may share one expansion offset.
        # Without a direct target, selecting one as the covering observation
        # would be arbitrary; retain the tree-sitter residual instead.
        if len(covered_candidates) == 1:
            idx, basis = covered_candidates[0]
            used_noninternal.add(idx)
            covered_by_noninternal += 1
            noninternal_rows[idx].setdefault("tree_sitter_evidence", []).append(
                {
                    "caller_title": edge.caller_title,
                    "target_title": edge.target_title,
                    "source_path": edge.source_path,
                    "line": edge.line,
                    "col0": edge.col0,
                    "byte_offset": edge.byte_offset,
                    "tree_sitter_span": edge.span,
                    "match_basis": basis,
                }
            )
            continue

        if edge.source_path not in in_scope_paths:
            out_of_scope.append(
                {
                    "caller_title": edge.caller_title,
                    "target_title": edge.target_title,
                    "source_path": edge.source_path,
                    "line": edge.line,
                    "col0": edge.col0,
                    "byte_offset": edge.byte_offset,
                    "tree_sitter_span": edge.span,
                    "classification": "out_of_compile_db_scope",
                }
            )
        else:
            ts_only.append(
                {
                    "caller_title": edge.caller_title,
                    "target_title": edge.target_title,
                    "source_path": edge.source_path,
                    "line": edge.line,
                    "col0": edge.col0,
                    "byte_offset": edge.byte_offset,
                    "tree_sitter_span": edge.span,
                    "classification": "tree_sitter_only_internal",
                }
            )

    for idx, row in enumerate(internal_rows):
        if idx not in used_internal:
            clang_only_internal.append(row)

    def sort_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            rows,
            key=lambda r: (
                str(r.get("caller_path") or r.get("source_path") or ""),
                str(r.get("caller_title") or r.get("caller_name") or ""),
                int(r.get("line") or 0),
                int(r.get("col0") if r.get("col0") is not None else -1),
                int(r.get("byte_offset") if r.get("byte_offset") is not None else -1),
                str(r.get("target_title") or r.get("target_name") or ""),
                str(r.get("classification") or ""),
                json.dumps(r, sort_keys=True),
            ),
        )

    buckets = {
        "matched_internal": sort_rows(matched_internal),
        "clang_only_internal": sort_rows(clang_only_internal),
        "tree_sitter_only_internal": sort_rows(ts_only),
        "external_direct": sort_rows(list(external_direct)),
        "indirect": sort_rows(list(indirect)),
        "ambiguous": sort_rows(list(ambiguous)),
        "macro_location_unsupported": sort_rows(list(macro_unsupported)),
        "out_of_compile_db_scope": sort_rows(out_of_scope),
    }
    counts = {k: len(v) for k, v in buckets.items()}
    return {
        "buckets": buckets,
        "counts": counts,
        "tree_sitter_accounting": {
            "total_calls": len(ts_edges),
            "matched_internal": len(matched_internal),
            "covered_by_noninternal_clang_observation": covered_by_noninternal,
            "tree_sitter_only_internal": len(ts_only),
            "out_of_compile_db_scope": len(out_of_scope),
        },
    }


# ---------------------------------------------------------------------------
# Full audit (builders consume a shared in-memory capture)
# ---------------------------------------------------------------------------


def build_call_audit_from_capture(capture: Any) -> Dict[str, Any]:
    """Build the call-site audit report from an in-memory capture.

    Never invokes the compiler or reloads ``compile_commands.json``.
    """
    from graphrag_code.c_clang_ast_capture import (  # type: ignore
        ClangAstCaptureError,
        ClangAstPackageCapture,
        assert_audit_report_matches_capture,
        validate_clang_ast_capture,
    )

    if not isinstance(capture, ClangAstPackageCapture):
        raise ClangCallAuditError(
            "build_call_audit_from_capture requires a ClangAstPackageCapture"
        )
    try:
        validate_clang_ast_capture(capture)
    except ClangAstCaptureError as e:
        raise ClangCallAuditError(str(e)) from e
    package_dir = capture.package_dir
    digest = capture.compile_commands_digest

    ts_edges, path_name_to_titles, _title_to_path, _data = collect_tree_sitter_calls(
        package_dir
    )

    all_calls: List[RawClangCall] = []
    translation_units: List[Dict[str, Any]] = []
    in_scope_paths: Set[str] = set()

    try:
        for ent in capture.entries:
            tu_path = ent.tu_path
            if path_is_under(tu_path, package_dir):
                in_scope_paths.add(package_relative_posix(tu_path, package_dir))

            calls, scope = collect_calls_from_ast(
                ent.ast_root,
                package_dir=package_dir,
                cwd=ent.cwd,
                entry_index=ent.entry_index,
                compiler_path=ent.compiler_path,
                compiler_id=ent.compiler_id,
                compile_commands_digest=digest,
                title_by_path_name=path_name_to_titles,
            )
            in_scope_paths |= scope
            all_calls.extend(calls)
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
                    "n_calls_observed": len(calls),
                }
            )
    except ClangAstCaptureError as e:
        raise ClangCallAuditError(str(e)) from e

    merged = merge_clang_calls(all_calls)
    compared = compare_calls(
        clang_rows=merged,
        ts_edges=ts_edges,
        in_scope_paths=in_scope_paths,
    )

    compiler_list = list(capture.compilers)
    one = compiler_list[0] if len(compiler_list) == 1 else {}
    buckets = compared["buckets"]
    counts = compared["counts"]

    report = {
        "mode": MODE,
        "package": package_dir.name,
        "compiler_path": one.get("compiler_path"),
        "compiler_id": one.get("compiler_id"),
        "compiler_version": one.get("compiler_version"),
        "compilers": compiler_list,
        "compile_commands_digest": digest,
        "n_compile_entries": capture.n_compile_entries,
        "n_clang_call_observations": len(all_calls),
        "n_merged_clang_call_records": len(merged),
        "translation_units": sorted(
            translation_units, key=lambda t: (t["entry_index"], str(t.get("file") or ""))
        ),
        "in_scope_source_paths": sorted(in_scope_paths),
        "column_convention": {
            "tree_sitter_call_span": "line:col with 1-based line and 0-based byte column",
            "clang_call_location": "CallExpr.range.begin; 1-based line/col when present",
            "primary_match_identity": "exact source byte offset",
            "location_fallback": (
                "exact line+normalized column only when either side lacks byte offset"
            ),
            "normalized_match_column": "clang_col0 = clang_col1 - 1",
        },
        "counts": counts,
        "tree_sitter_accounting": compared["tree_sitter_accounting"],
        "matched_internal": buckets["matched_internal"],
        "clang_only_internal": buckets["clang_only_internal"],
        "tree_sitter_only_internal": buckets["tree_sitter_only_internal"],
        "external_direct": buckets["external_direct"],
        "indirect": buckets["indirect"],
        "ambiguous": buckets["ambiguous"],
        "macro_location_unsupported": buckets["macro_location_unsupported"],
        "out_of_compile_db_scope": buckets["out_of_compile_db_scope"],
        "limitations": [
            "Diagnostic only — no BYOG call facts published",
            (
                "Direct internal requires DeclRefExpr -> FunctionDecl with "
                "unambiguous package definition"
            ),
            "Callee is CallExpr.inner[0] only; argument DeclRefExpr nodes are ignored",
            "Indirect calls (ParmVarDecl/VarDecl/MemberExpr) are not points-to resolved",
            "External direct calls are observations, not internal mismatches",
            "CallExpr location from range.begin; Apple clang omits CallExpr.loc",
            "Byte offsets are primary; no unsafe column-only matching is performed",
            "Column normalization: Clang 1-based -> 0-based for strict fallback compare",
            "Not multi-config, not C++, not full macro expansion fidelity",
        ],
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }
    normalized = _normalize_report(report)
    try:
        assert_audit_report_matches_capture(
            normalized, capture, context="call audit"
        )
    except ClangAstCaptureError as e:
        raise ClangCallAuditError(str(e)) from e
    return normalized


def run_clang_call_audit(
    package_dir: Path,
    *,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Run the call-site audit; one AST dump per compile entry.

    Internally creates one shared AST capture then builds the call report.
    """
    from graphrag_code.c_clang_ast_capture import (  # type: ignore
        ClangAstCaptureError,
        capture_clang_ast_package,
    )

    if timeout <= 0:
        raise ClangCallAuditError("timeout must be a positive integer")
    try:
        capture = capture_clang_ast_package(package_dir, timeout=timeout)
    except ClangAstCaptureError as e:
        raise ClangCallAuditError(str(e)) from e
    return build_call_audit_from_capture(capture)


def _normalize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    text = json.dumps(report, sort_keys=True, separators=(",", ":"))
    return json.loads(text)


def audit_to_json(report: Dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, indent=2) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Clang AST JSON call-site audit vs tree-sitter-c calls "
            "(diagnostic only; no BYOG mutation)."
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
        "--fail-on-internal-mismatch",
        action="store_true",
        help=(
            "Exit 1 when clang_only_internal, tree_sitter_only_internal, "
            "ambiguous, or macro_location_unsupported counts are non-zero "
            "(external/indirect alone do not fail)"
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
        report = run_clang_call_audit(args.package, timeout=args.timeout)
    except ClangCallAuditError as e:
        print(f"c_clang_call_audit: {e}", file=sys.stderr)
        return 2
    except ClangAstAuditError as e:
        print(f"c_clang_call_audit: {e}", file=sys.stderr)
        return 2

    text = audit_to_json(report)
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        except OSError as e:
            print(f"c_clang_call_audit: failed to write output: {e}", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(text)

    if args.fail_on_internal_mismatch:
        counts = report.get("counts") or {}
        residual = (
            int(counts.get("clang_only_internal") or 0)
            + int(counts.get("tree_sitter_only_internal") or 0)
            + int(counts.get("ambiguous") or 0)
            + int(counts.get("macro_location_unsupported") or 0)
        )
        if residual:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
