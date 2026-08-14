#!/usr/bin/env python
"""Clang AST JSON function-definition audit (diagnostic only).

Compares package-local Clang ``FunctionDecl`` *definitions* from
``compile_commands.json`` translation units against tree-sitter-c function
entities from ``build_c_byog``.

This is **not** a graph overlay: it does not publish AST facts into BYOG and
does not add flags to ``index_c.py``.

Clang only. GCC/MSVC/wrappers fail explicitly. Signatures and locations are
configuration/toolchain-derived relative to the recorded compile DB.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from graphrag_code.c_compiler_common import (  # type: ignore
    CompilerOverlayError,
    path_is_under,
    prepare_compile_entry,
    reject_hidden_compiler_outputs,
    # Re-export for tests/consumers that historically imported these here.
    require_clang_identity,
)
from graphrag_code.c_identities import INDEXED_SUFFIXES, package_relative_posix  # type: ignore
from graphrag_code.extract_c import build_c_byog  # type: ignore

MODE = "clang_ast_json_audit"
MAX_AST_JSON_BYTES = 256 * 1024 * 1024
CONFIDENCE_BOUNDARY = (
    "Signatures and match classifications are configuration/toolchain-derived "
    "from the recorded Clang + compile_commands.json only. Macro spelling vs "
    "expansion disagreement, compile-DB coverage gaps, and tree-sitter parse "
    "limits remain explicit residuals — not published graph facts."
)

_SPAN_RE = re.compile(
    r"^(?P<sl>\d+):(?P<sc>\d+)-(?P<el>\d+):(?P<ec>\d+)$"
)


class ClangAstAuditError(CompilerOverlayError):
    """Raised when the AST audit cannot run honestly."""


# ---------------------------------------------------------------------------
# Pure AST parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceLocation:
    file: Optional[str]
    line: Optional[int]
    col: Optional[int]
    origin: str  # direct | spelling | expansion | inherited | unresolved


@dataclass
class ClangFunctionDefinition:
    name: str
    source_path: str  # package-relative POSIX when package-local
    line: Optional[int]
    col: Optional[int]
    qual_type: str
    storage_class: Optional[str]
    inline: Optional[bool]
    variadic: Optional[bool]
    mangled_name: Optional[str]
    location_origin: str
    spelling_path: Optional[str] = None
    expansion_path: Optional[str] = None
    is_package_local: bool = True
    classification_hint: Optional[str] = None  # e.g. macro_location_unsupported
    entry_indices: List[int] = field(default_factory=list)
    compiler_path: Optional[str] = None
    compiler_id: Optional[str] = None
    compile_commands_digest: Optional[str] = None
    observation_variants: List[Dict[str, Any]] = field(default_factory=list)

    def identity_key(self) -> Tuple[str, str]:
        return (self.source_path, self.name)


def _loc_field(node: Optional[Dict[str, Any]], key: str) -> Optional[Any]:
    if not isinstance(node, dict):
        return None
    return node.get(key)


def _has_function_body(node: Dict[str, Any]) -> bool:
    """Definition test for Apple/LLVM AST JSON (no isThisDeclarationADefinition).

    Local probe: definitions carry a ``CompoundStmt`` child; prototypes do not.
    """
    for child in node.get("inner") or []:
        if isinstance(child, dict) and child.get("kind") == "CompoundStmt":
            return True
    return False


def resolve_function_location(
    loc: Optional[Dict[str, Any]],
    *,
    last_file: Optional[str],
    range_begin: Optional[Dict[str, Any]] = None,
) -> Tuple[SourceLocation, SourceLocation, SourceLocation]:
    """Return (primary, spelling, expansion) locations.

    Primary prefers a non-macro loc with file/line; macro nodes expose both
    spelling and expansion. Omitted ``file`` inherits ``last_file`` only for
    ordinary (non-macro) locations — matching probed Clang omission of
    repeated file fields within a TU.
    """
    empty = SourceLocation(None, None, None, "unresolved")
    if not isinstance(loc, dict):
        # range.begin fallback
        if isinstance(range_begin, dict):
            return resolve_function_location(
                range_begin, last_file=last_file, range_begin=None
            )
        return empty, empty, empty

    if "spellingLoc" in loc or "expansionLoc" in loc:
        spell = loc.get("spellingLoc") if isinstance(loc.get("spellingLoc"), dict) else {}
        exp = loc.get("expansionLoc") if isinstance(loc.get("expansionLoc"), dict) else {}
        spell_file = spell.get("file") or last_file
        exp_file = exp.get("file") or last_file
        spelling = SourceLocation(
            str(spell_file) if spell_file else None,
            spell.get("line"),
            spell.get("col"),
            "spelling",
        )
        expansion = SourceLocation(
            str(exp_file) if exp_file else None,
            exp.get("line"),
            exp.get("col"),
            "expansion",
        )
        # Primary for reporting: expansion if it has line, else spelling.
        primary = expansion if expansion.line is not None else spelling
        if primary.file is None and last_file:
            primary = SourceLocation(
                last_file, primary.line, primary.col, primary.origin
            )
        return primary, spelling, expansion

    file_val = loc.get("file")
    origin = "direct"
    if not file_val:
        file_val = last_file
        origin = "inherited" if last_file else "unresolved"
    if not file_val and isinstance(range_begin, dict) and range_begin.get("file"):
        file_val = range_begin.get("file")
        origin = "direct"
    primary = SourceLocation(
        str(file_val) if file_val else None,
        loc.get("line") if loc.get("line") is not None else _loc_field(range_begin, "line"),
        loc.get("col") if loc.get("col") is not None else _loc_field(range_begin, "col"),
        origin,
    )
    return primary, empty, empty


def _normalize_path_token(
    raw: Optional[str],
    *,
    cwd: Path,
    package_dir: Path,
) -> Tuple[Optional[str], Optional[Path], bool]:
    """Return (package-relative POSIX or None, resolved path, is_package_local)."""
    if not raw:
        return None, None, False
    p = Path(str(raw))
    if not p.is_absolute():
        p = (cwd / p).resolve()
    else:
        p = p.resolve()
    if not path_is_under(p, package_dir):
        return None, p, False
    if p.suffix not in INDEXED_SUFFIXES:
        return None, p, False
    rel = package_relative_posix(p, package_dir)
    return rel, p, True


def collect_function_definitions_from_ast(
    root: Any,
    *,
    package_dir: Path,
    cwd: Path,
    entry_index: int,
    compiler_path: str,
    compiler_id: Optional[str],
    compile_commands_digest: str,
) -> List[ClangFunctionDefinition]:
    """Walk one AST JSON root; return package-local FunctionDecl definitions.

    Location policy (from Apple clang JSON dump probes):
    * ``file`` is omitted when it matches the previous *non-implicit* dump
      location; we therefore carry ``last_file`` across the walk.
    * Implicit nodes (compiler builtins like ``__builtin_nanf``) must **not**
      update file context — their spellingLoc points at system headers and
      would otherwise re-parent subsequent package definitions.
    * Definition test: non-implicit ``FunctionDecl`` with a ``CompoundStmt``
      body (``isThisDeclarationADefinition`` is absent on this host).
    """
    package_dir = package_dir.resolve()
    cwd = cwd.resolve()
    out: List[ClangFunctionDefinition] = []

    def walk(node: Any, last_file: Optional[str]) -> Optional[str]:
        if not isinstance(node, dict):
            return last_file

        # Implicit builtins/system noise: do not touch file context or collect.
        if node.get("isImplicit"):
            return last_file

        loc = node.get("loc") if isinstance(node.get("loc"), dict) else None
        # Only advance context from *explicit* file fields (not macro nesting
        # alone), so spellingLoc into float.h on a non-implicit node still
        # updates — but builtins are already skipped above.
        if isinstance(loc, dict) and loc.get("file"):
            last_file = str(loc["file"])
        elif isinstance(loc, dict):
            # Macro location on a real decl: prefer expansion/spelling file if any.
            for key in ("expansionLoc", "spellingLoc"):
                nested = loc.get(key)
                if isinstance(nested, dict) and nested.get("file"):
                    last_file = str(nested["file"])
                    break

        rng = node.get("range") if isinstance(node.get("range"), dict) else None
        range_begin = rng.get("begin") if isinstance(rng, dict) else None

        if (
            node.get("kind") == "FunctionDecl"
            and node.get("name")
            and _has_function_body(node)
        ):
            primary, spelling, expansion = resolve_function_location(
                loc, last_file=last_file, range_begin=range_begin
            )
            if primary.file:
                last_file = primary.file

            spell_rel, _, spell_local = _normalize_path_token(
                spelling.file, cwd=cwd, package_dir=package_dir
            )
            exp_rel, _, exp_local = _normalize_path_token(
                expansion.file, cwd=cwd, package_dir=package_dir
            )
            prim_rel, _, prim_local = _normalize_path_token(
                primary.file, cwd=cwd, package_dir=package_dir
            )

            hint = None
            source_path: Optional[str] = None
            origin = primary.origin
            line, col = primary.line, primary.col
            # Macro spelling/expansion disagree on package file → unsupported.
            if (
                spelling.origin == "spelling"
                and expansion.origin == "expansion"
                and spell_local
                and exp_local
                and spell_rel
                and exp_rel
                and spell_rel != exp_rel
            ):
                hint = "macro_location_unsupported"
                source_path = spell_rel
                origin = "spelling+expansion"
                line, col = spelling.line, spelling.col
            elif prim_local and prim_rel:
                source_path = prim_rel
            # else: outside package — leave context, do not collect.

            if source_path is not None:
                type_node = (
                    node.get("type")
                    if isinstance(node.get("type"), dict)
                    else {}
                )
                out.append(
                    ClangFunctionDefinition(
                        name=str(node.get("name")),
                        source_path=source_path,
                        line=int(line) if line is not None else None,
                        col=int(col) if col is not None else None,
                        qual_type=str(type_node.get("qualType") or ""),
                        storage_class=node.get("storageClass"),
                        inline=node.get("inline") if "inline" in node else None,
                        variadic=(
                            node.get("variadic") if "variadic" in node else None
                        ),
                        mangled_name=node.get("mangledName"),
                        location_origin=origin,
                        spelling_path=spell_rel,
                        expansion_path=exp_rel,
                        is_package_local=True,
                        classification_hint=hint,
                        entry_indices=[entry_index],
                        compiler_path=compiler_path,
                        compiler_id=compiler_id,
                        compile_commands_digest=compile_commands_digest,
                    )
                )

        for child in node.get("inner") or []:
            last_file = walk(child, last_file)
        return last_file

    walk(root, None)
    return out


def parse_ast_json_document(text: str) -> Any:
    """Parse a single AST JSON root; fail on empty or multiple concatenated roots."""
    if not text or not text.strip():
        raise ClangAstAuditError("compiler AST dump produced empty stdout")
    decoder = json.JSONDecoder()
    raw = text.lstrip()
    try:
        obj, idx = decoder.raw_decode(raw)
    except json.JSONDecodeError as e:
        raise ClangAstAuditError(
            f"malformed Clang AST JSON: {e}"
        ) from e
    rest = raw[idx:].strip()
    if rest:
        raise ClangAstAuditError(
            "Clang AST dump contains multiple concatenated JSON roots; "
            "refusing to guess document boundaries"
        )
    if not isinstance(obj, dict):
        raise ClangAstAuditError(
            f"Clang AST JSON root must be an object, got {type(obj).__name__}"
        )
    return obj


# ---------------------------------------------------------------------------
# Compiler invocation
# ---------------------------------------------------------------------------


def ast_dump_command_from_entry(
    entry: Dict[str, Any],
    *,
    compiler: str,
    package_dir: Path,
) -> Tuple[Path, List[str], Path]:
    """Build ``clang -fsyntax-only -Xclang -ast-dump=json …`` argv."""
    try:
        cwd, cleaned, src_path = prepare_compile_entry(
            entry, package_dir=package_dir
        )
        reject_hidden_compiler_outputs(cleaned)
    except CompilerOverlayError as e:
        raise ClangAstAuditError(str(e)) from e
    argv = [
        compiler,
        "-fsyntax-only",
        "-Xclang",
        "-ast-dump=json",
        *cleaned,
    ]
    return cwd, argv, src_path


def run_ast_dump_for_entry(
    entry: Dict[str, Any],
    *,
    compiler: str,
    package_dir: Path,
    timeout: int = 120,
) -> Tuple[Path, Any]:
    """Run Clang AST dump for one entry; return (tu_path, parsed JSON root)."""
    cwd, argv, tu_path = ast_dump_command_from_entry(
        entry, compiler=compiler, package_dir=package_dir
    )
    try:
        with tempfile.TemporaryFile(mode="w+b") as ast_out:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                stdout=ast_out,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                timeout=timeout,
            )
            ast_out.flush()
            ast_out.seek(0, 2)
            ast_size = ast_out.tell()
            if ast_size > MAX_AST_JSON_BYTES:
                raise ClangAstAuditError(
                    "Clang AST dump exceeds the audited size limit "
                    f"({ast_size} > {MAX_AST_JSON_BYTES} bytes)"
                )
            ast_out.seek(0)
            ast_text = ast_out.read().decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        raise ClangAstAuditError(
            f"Clang AST dump timed out: {' '.join(argv)}"
        ) from e
    except OSError as e:
        raise ClangAstAuditError(
            f"failed to invoke Clang for AST dump: {e}"
        ) from e
    if proc.returncode != 0:
        raise ClangAstAuditError(
            f"Clang AST dump failed ({proc.returncode}): {' '.join(argv)}\n"
            f"{(proc.stderr or '')[:800]}"
        )
    root = parse_ast_json_document(ast_text)
    return tu_path.resolve(), root


# ---------------------------------------------------------------------------
# Tree-sitter side + matching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TreeSitterFunction:
    title: str
    name: str
    source_path: str  # package-relative
    line: Optional[int]
    col: Optional[int]
    preprocessor_dependent: bool
    preprocessor_reasons: Tuple[str, ...]
    preprocessor_branches: Tuple[Any, ...]


def _parse_span_start(span: str) -> Tuple[Optional[int], Optional[int]]:
    m = _SPAN_RE.match(str(span or ""))
    if not m:
        return None, None
    return int(m.group("sl")), int(m.group("sc"))


def collect_tree_sitter_functions(
    package_dir: Path,
) -> List[TreeSitterFunction]:
    package_dir = package_dir.resolve()
    data = build_c_byog(package_dir)
    out: List[TreeSitterFunction] = []
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
        try:
            p = Path(sf)
            if not p.is_absolute():
                p = (package_dir / p).resolve()
            else:
                p = p.resolve()
            rel = package_relative_posix(p, package_dir)
        except (OSError, ValueError) as e:
            raise ClangAstAuditError(
                "tree-sitter function has a source path outside the audited "
                f"package: {sf!r}"
            ) from e
        line, col = _parse_span_start(str(e.get("span") or ""))
        reasons = e.get("preprocessor_reasons") or []
        if not isinstance(reasons, list):
            reasons = [reasons]
        branches = e.get("preprocessor_branches") or []
        if not isinstance(branches, list):
            branches = []
        out.append(
            TreeSitterFunction(
                title=title,
                name=name,
                source_path=rel,
                line=line,
                col=col,
                preprocessor_dependent=bool(e.get("preprocessor_dependent")),
                preprocessor_reasons=tuple(str(r) for r in reasons),
                preprocessor_branches=tuple(
                    json.dumps(b, sort_keys=True) if isinstance(b, dict) else str(b)
                    for b in branches
                ),
            )
        )
    out.sort(key=lambda t: (t.source_path, t.name, t.line or 0, t.col or 0, t.title))
    return out


def _merge_clang_defs(
    defs: Sequence[ClangFunctionDefinition],
) -> List[ClangFunctionDefinition]:
    """Deduplicate observations without hiding configuration disagreement."""

    def semantic_record(d: ClangFunctionDefinition) -> Dict[str, Any]:
        return {
            "line": d.line,
            "col": d.col,
            "qualType": d.qual_type,
            "storageClass": d.storage_class,
            "inline": d.inline,
            "variadic": d.variadic,
            "mangledName": d.mangled_name,
            "location_origin": d.location_origin,
            "spelling_path": d.spelling_path,
            "expansion_path": d.expansion_path,
            "classification_hint": d.classification_hint,
        }

    groups: Dict[Tuple[str, str], List[ClangFunctionDefinition]] = {}
    for d in defs:
        groups.setdefault(d.identity_key(), []).append(d)

    by_key: Dict[Tuple[str, str], ClangFunctionDefinition] = {}
    for key, rows in groups.items():
        semantic_keys = {
            json.dumps(semantic_record(d), sort_keys=True, separators=(",", ":"))
            for d in rows
        }

        # Preserve the actual compiler + entry indices for every distinct
        # observation, even when its semantic result equals another entry.
        observations: Dict[str, Dict[str, Any]] = {}
        for d in rows:
            rec = {
                **semantic_record(d),
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
                    **semantic_record(d),
                    "compiler_path": d.compiler_path,
                    "compiler_id": d.compiler_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        hint = chosen.classification_hint
        if len(semantic_keys) > 1:
            hint = "conflicting_compile_observations"
        by_key[key] = ClangFunctionDefinition(
            name=chosen.name,
            source_path=chosen.source_path,
            line=chosen.line,
            col=chosen.col,
            qual_type=chosen.qual_type,
            storage_class=chosen.storage_class,
            inline=chosen.inline,
            variadic=chosen.variadic,
            mangled_name=chosen.mangled_name,
            location_origin=chosen.location_origin,
            spelling_path=chosen.spelling_path,
            expansion_path=chosen.expansion_path,
            is_package_local=chosen.is_package_local,
            classification_hint=hint,
            entry_indices=sorted(
                {i for d in rows for i in d.entry_indices}
            ),
            compiler_path=chosen.compiler_path,
            compiler_id=chosen.compiler_id,
            compile_commands_digest=chosen.compile_commands_digest,
            observation_variants=[observations[k] for k in sorted(observations)],
        )
    return sorted(
        by_key.values(),
        key=lambda d: (d.source_path, d.name, d.line or 0, d.col or 0),
    )


def _ts_config_evidence(ts: TreeSitterFunction) -> Dict[str, Any]:
    """Surface preprocessor liveness evidence without inventing exclusions."""
    dead = any(
        str(b).find('"liveness": "dead"') >= 0
        or (isinstance(b, str) and "branch_dead" in b)
        for b in ts.preprocessor_branches
    )
    # reasons may contain branch_dead:...
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


def match_definitions(
    *,
    clang_defs: Sequence[ClangFunctionDefinition],
    tree_sitter: Sequence[TreeSitterFunction],
    in_scope_paths: Set[str],
) -> Dict[str, Any]:
    """Classify definitions into the audit buckets."""
    merged = _merge_clang_defs(clang_defs)

    ambiguous: List[Dict[str, Any]] = []
    macro_unsupported: List[Dict[str, Any]] = []
    clang_matchable: List[ClangFunctionDefinition] = []
    classified_ts_keys: Set[Tuple[str, str]] = set()

    for d in merged:
        if d.classification_hint == "macro_location_unsupported":
            macro_unsupported.append(
                {
                    "name": d.name,
                    "source_path": d.source_path,
                    "spelling_path": d.spelling_path,
                    "expansion_path": d.expansion_path,
                    "line": d.line,
                    "col": d.col,
                    "qualType": d.qual_type,
                    "entry_indices": list(d.entry_indices),
                    "observations": d.observation_variants,
                    "reason": "spelling and expansion resolve to different package files",
                }
            )
            classified_ts_keys.add(d.identity_key())
            if d.spelling_path:
                classified_ts_keys.add((d.spelling_path, d.name))
            if d.expansion_path:
                classified_ts_keys.add((d.expansion_path, d.name))
            continue
        if d.classification_hint == "conflicting_compile_observations":
            ambiguous.append(
                {
                    "name": d.name,
                    "source_path": d.source_path,
                    "reason": "compile entries produced conflicting Clang observations",
                    "entry_indices": list(d.entry_indices),
                    "observations": d.observation_variants,
                }
            )
            classified_ts_keys.add(d.identity_key())
            continue
        clang_matchable.append(d)

    # Index tree-sitter by (path, name)
    ts_by_key: Dict[Tuple[str, str], List[TreeSitterFunction]] = {}
    for t in tree_sitter:
        ts_by_key.setdefault((t.source_path, t.name), []).append(t)

    matched: List[Dict[str, Any]] = []
    tree_sitter_only: List[Dict[str, Any]] = []
    clang_only: List[Dict[str, Any]] = []
    out_of_scope: List[Dict[str, Any]] = []

    matched_ts_keys: Set[Tuple[str, str]] = set()
    for d in clang_matchable:
        key = d.identity_key()
        candidates = ts_by_key.get(key, [])
        if not candidates:
            clang_only.append(
                {
                    "name": d.name,
                    "source_path": d.source_path,
                    "line": d.line,
                    "col": d.col,
                    "qualType": d.qual_type,
                    "storageClass": d.storage_class,
                    "inline": d.inline,
                    "variadic": d.variadic,
                    "mangledName": d.mangled_name,
                    "location_origin": d.location_origin,
                    "entry_indices": list(d.entry_indices),
                    "compiler_path": d.compiler_path,
                    "compiler_id": d.compiler_id,
                    "compile_commands_digest": d.compile_commands_digest,
                    "observations": d.observation_variants,
                }
            )
            continue
        if len(candidates) > 1:
            # Same name+path twice in tree-sitter (should be rare) → ambiguous.
            ambiguous.append(
                {
                    "name": d.name,
                    "source_path": d.source_path,
                    "reason": "multiple tree-sitter function entities share path+name",
                    "tree_sitter_titles": sorted(c.title for c in candidates),
                    "clang_line": d.line,
                    "clang_col": d.col,
                    "entry_indices": list(d.entry_indices),
                    "observations": d.observation_variants,
                }
            )
            classified_ts_keys.add(key)
            continue
        ts = candidates[0]
        if (
            d.line is not None
            and ts.line is not None
            and abs(d.line - ts.line) > 2
        ):
            ambiguous.append(
                {
                    "name": d.name,
                    "source_path": d.source_path,
                    "reason": "path+name agree but source lines disagree",
                    "tree_sitter_title": ts.title,
                    "tree_sitter_line": ts.line,
                    "tree_sitter_col": ts.col,
                    "clang_line": d.line,
                    "clang_col": d.col,
                    "entry_indices": list(d.entry_indices),
                    "observations": d.observation_variants,
                }
            )
            classified_ts_keys.add(key)
            continue
        line_confirm = (
            d.line is not None
            and ts.line is not None
            and abs(d.line - ts.line) <= 2
        )
        matched.append(
            {
                "name": d.name,
                "source_path": d.source_path,
                "tree_sitter_title": ts.title,
                "tree_sitter_line": ts.line,
                "tree_sitter_col": ts.col,
                "clang_line": d.line,
                "clang_col": d.col,
                "line_column_confirmed": line_confirm,
                "qualType": d.qual_type,
                "storageClass": d.storage_class,
                "inline": d.inline,
                "variadic": d.variadic,
                "mangledName": d.mangled_name,
                "location_origin": d.location_origin,
                "entry_indices": list(d.entry_indices),
                "compiler_path": d.compiler_path,
                "compiler_id": d.compiler_id,
                "compile_commands_digest": d.compile_commands_digest,
                "observations": d.observation_variants,
            }
        )
        matched_ts_keys.add(key)

    for t in tree_sitter:
        key = (t.source_path, t.name)
        if key in matched_ts_keys or key in classified_ts_keys:
            continue
        if t.source_path not in in_scope_paths:
            out_of_scope.append(
                {
                    "name": t.name,
                    "source_path": t.source_path,
                    "tree_sitter_title": t.title,
                    "line": t.line,
                    "col": t.col,
                    "classification": "out_of_compile_db_scope",
                    **_ts_config_evidence(t),
                }
            )
            continue
        # In scope but no clang definition
        rec = {
            "name": t.name,
            "source_path": t.source_path,
            "tree_sitter_title": t.title,
            "line": t.line,
            "col": t.col,
            "classification": "tree_sitter_only",
            **_ts_config_evidence(t),
        }
        tree_sitter_only.append(rec)

    def sort_recs(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            rows,
            key=lambda r: (
                str(r.get("source_path") or ""),
                str(r.get("name") or ""),
                str(r.get("tree_sitter_title") or ""),
                int(r.get("line") or r.get("clang_line") or 0),
                int(r.get("col") or r.get("clang_col") or 0),
                json.dumps(r, sort_keys=True),
            ),
        )

    in_scope_ts = [
        t for t in tree_sitter if t.source_path in in_scope_paths
    ]

    return {
        "matched": sort_recs(matched),
        "tree_sitter_only": sort_recs(tree_sitter_only),
        "clang_only": sort_recs(clang_only),
        "ambiguous": sort_recs(ambiguous),
        "macro_location_unsupported": sort_recs(macro_unsupported),
        "out_of_compile_db_scope": sort_recs(out_of_scope),
        "counts": {
            "matched": len(matched),
            "tree_sitter_only": len(tree_sitter_only),
            "clang_only": len(clang_only),
            "ambiguous": len(ambiguous),
            "macro_location_unsupported": len(macro_unsupported),
            "out_of_compile_db_scope": len(out_of_scope),
            "clang_definitions_package_local": len(merged),
            "tree_sitter_definitions_total": len(tree_sitter),
            "tree_sitter_definitions_in_scope": len(in_scope_ts),
        },
    }


# ---------------------------------------------------------------------------
# Full audit (builders consume a shared in-memory capture)
# ---------------------------------------------------------------------------


def build_function_audit_from_capture(capture: Any) -> Dict[str, Any]:
    """Build the function-definition audit report from an in-memory capture.

    Never invokes the compiler or reloads ``compile_commands.json``.
    """
    # Local import avoids cycles with c_clang_ast_capture → this module.
    from graphrag_code.c_clang_ast_capture import (  # type: ignore
        ClangAstCaptureError,
        ClangAstPackageCapture,
        assert_audit_report_matches_capture,
        validate_clang_ast_capture,
    )

    if not isinstance(capture, ClangAstPackageCapture):
        raise ClangAstAuditError(
            "build_function_audit_from_capture requires a ClangAstPackageCapture"
        )
    try:
        validate_clang_ast_capture(capture)
    except ClangAstCaptureError as e:
        raise ClangAstAuditError(str(e)) from e
    package_dir = capture.package_dir
    digest = capture.compile_commands_digest

    all_defs: List[ClangFunctionDefinition] = []
    translation_units: List[Dict[str, Any]] = []
    in_scope_paths: Set[str] = set()

    try:
        for ent in capture.entries:
            tu_path = ent.tu_path
            if path_is_under(tu_path, package_dir):
                in_scope_paths.add(package_relative_posix(tu_path, package_dir))

            defs = collect_function_definitions_from_ast(
                ent.ast_root,
                package_dir=package_dir,
                cwd=ent.cwd,
                entry_index=ent.entry_index,
                compiler_path=ent.compiler_path,
                compiler_id=ent.compiler_id,
                compile_commands_digest=digest,
            )
            for d in defs:
                if d.is_package_local:
                    in_scope_paths.add(d.source_path)
            all_defs.extend(defs)
            tu_is_local = path_is_under(tu_path, package_dir)
            translation_units.append(
                {
                    "entry_index": ent.entry_index,
                    "file": package_relative_posix(tu_path, package_dir)
                    if tu_is_local
                    else None,
                    "package_local": tu_is_local,
                    "compiler_path": ent.compiler_path,
                    "compiler_id": ent.compiler_id,
                    "n_package_local_definitions": len(
                        [d for d in defs if d.is_package_local]
                    ),
                }
            )
    except ClangAstCaptureError as e:
        raise ClangAstAuditError(str(e)) from e

    ts_fns = collect_tree_sitter_functions(package_dir)
    classes = match_definitions(
        clang_defs=all_defs,
        tree_sitter=ts_fns,
        in_scope_paths=in_scope_paths,
    )

    compiler_list = list(capture.compilers)
    one = compiler_list[0] if len(compiler_list) == 1 else {}

    report = {
        "mode": MODE,
        "package": package_dir.name,
        "compiler_path": one.get("compiler_path"),
        "compiler_id": one.get("compiler_id"),
        "compiler_version": one.get("compiler_version"),
        "compilers": compiler_list,
        "compile_commands_digest": digest,
        "n_compile_entries": capture.n_compile_entries,
        "translation_units": sorted(
            translation_units, key=lambda t: (t["entry_index"], t["file"])
        ),
        "in_scope_source_paths": sorted(in_scope_paths),
        "counts": classes["counts"],
        "matched": classes["matched"],
        "tree_sitter_only": classes["tree_sitter_only"],
        "clang_only": classes["clang_only"],
        "ambiguous": classes["ambiguous"],
        "macro_location_unsupported": classes["macro_location_unsupported"],
        "out_of_compile_db_scope": classes["out_of_compile_db_scope"],
        "limitations": [
            "Clang AST JSON only (Apple Clang / LLVM); GCC and MSVC refused",
            "Definition = FunctionDecl with CompoundStmt body (probed field set)",
            "isThisDeclarationADefinition not present in probed Apple clang dumps",
            "Match key = package-relative path + function name; known line disagreement is ambiguous",
            "Files never observed as TU or package-local AST definition sites are out_of_compile_db_scope",
            "Conflicting observations for one path+name across compile entries are ambiguous",
            "Macro spelling/expansion multi-file disagreement is not silently matched",
            "Not a BYOG graph overlay; no AST facts published",
        ],
        "confidence_boundary": CONFIDENCE_BOUNDARY,
    }
    normalized = _normalize_report(report)
    try:
        assert_audit_report_matches_capture(
            normalized, capture, context="function audit"
        )
    except ClangAstCaptureError as e:
        raise ClangAstAuditError(str(e)) from e
    return normalized


def run_clang_ast_audit(
    package_dir: Path,
    *,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Run the full package audit; returns a deterministic JSON-ready dict.

    Internally creates one shared AST capture (one dump per compile entry)
    then builds the function-definition report from it.
    """
    from graphrag_code.c_clang_ast_capture import (  # type: ignore
        ClangAstCaptureError,
        capture_clang_ast_package,
    )

    try:
        capture = capture_clang_ast_package(package_dir, timeout=timeout)
    except ClangAstCaptureError as e:
        raise ClangAstAuditError(str(e)) from e
    return build_function_audit_from_capture(capture)


def _normalize_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Deep-sort lists for PYTHONHASHSEED-independent serialization."""
    # Top-level already sorted; ensure nested reason arrays sorted.
    text = json.dumps(report, sort_keys=True, separators=(",", ":"))
    return json.loads(text)


def audit_to_json(report: Dict[str, Any]) -> str:
    """Canonical JSON text (sorted keys, stable separators, trailing newline)."""
    return json.dumps(report, sort_keys=True, indent=2) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Clang AST JSON function-definition audit against tree-sitter-c "
            "(diagnostic only; no BYOG graph mutation)."
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
            "Exit 1 when tree_sitter_only/clang_only/ambiguous/"
            "macro_location_unsupported counts are non-zero "
            "(out_of_compile_db_scope alone does not fail)"
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
        report = run_clang_ast_audit(args.package, timeout=args.timeout)
    except ClangAstAuditError as e:
        print(f"c_clang_ast_audit: {e}", file=sys.stderr)
        return 2

    text = audit_to_json(report)
    if args.output is not None:
        try:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        except OSError as e:
            print(f"c_clang_ast_audit: cannot write report: {e}", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(text)

    if args.fail_on_mismatch:
        counts = report.get("counts") or {}
        residual = (
            int(counts.get("tree_sitter_only") or 0)
            + int(counts.get("clang_only") or 0)
            + int(counts.get("ambiguous") or 0)
            + int(counts.get("macro_location_unsupported") or 0)
        )
        if residual:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
