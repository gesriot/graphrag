#!/usr/bin/env python
"""
Minimal C frontend for BYOG (Phase 6 bootstrap, tree-sitter-c).

Builds a BYOG graph directly (final schema, no Python bridge) from a C package:
- entities: file, function, struct, enum, typedef (with spans + provenance)
- relationships: contains (file -> symbol), calls (intra-package function calls)
- text_units: source snippet per entity
- call_observations: calls to undefined/external functions (weak, honest unknowns)

Scope note (measured on jsmn): tree-sitter-c parses header-only / macro'd C with a
few ERROR nodes (e.g. the JSMN_API macro); per Plan, clang + compile_commands is
the eventual route for macro/include/type accuracy. This bootstrap stays
conservative: only calls whose callee is a function defined in the package become
deterministic CALLS edges. Same-file definitions win when duplicate C function
names exist; otherwise ambiguous or external calls stay observations.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from tree_sitter import Language, Node, Parser  # type: ignore
import tree_sitter_c as tsc  # type: ignore

from c_identities import (  # type: ignore
    build_module_key_map,
    file_entity_title,
    list_indexed_c_files,
    symbol_entity_title,
)

_LANG = Language(tsc.language())

# C reserved words. tree-sitter-c does not evaluate the preprocessor, so a
# function body fragmented by `#if`/`#endif` (e.g. inih's `else if (cond) {..}`)
# can be misparsed as a `function_definition` whose "name" is a control keyword.
# A real C function can never be named a reserved word, so reject these outright.
_C_KEYWORDS = frozenset({
    "alignas",
    "alignof",
    "auto",
    "bool",
    "break",
    "case",
    "char",
    "const",
    "constexpr",
    "continue",
    "default",
    "do",
    "double",
    "else",
    "enum",
    "extern",
    "false",
    "float",
    "for",
    "goto",
    "if",
    "inline",
    "int",
    "long",
    "nullptr",
    "register",
    "restrict",
    "return",
    "short",
    "signed",
    "sizeof",
    "static",
    "static_assert",
    "struct",
    "switch",
    "thread_local",
    "true",
    "typedef",
    "typeof",
    "typeof_unqual",
    "union",
    "unsigned",
    "void",
    "volatile",
    "while",
    "_Alignas",
    "_Alignof",
    "_Atomic",
    "_BitInt",
    "_Bool",
    "_Complex",
    "_Decimal128",
    "_Decimal32",
    "_Decimal64",
    "_Generic",
    "_Imaginary",
    "_Noreturn",
    "_Static_assert",
    "_Thread_local",
})


def _parser() -> Parser:
    return Parser(_LANG)


def _text(src: bytes, node: Node) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _span(node: Node) -> str:
    return (
        f"{node.start_point[0] + 1}:{node.start_point[1]}-"
        f"{node.end_point[0] + 1}:{node.end_point[1]}"
    )


def _slug(title: str) -> str:
    """Lossy display token; never the sole unique id component for new keys."""
    return re.sub(r"[^0-9A-Za-z_.]", "_", title)


def _call_rel_id(
    module_key: str, caller: str, callee: str, line: int, col: int
) -> str:
    """Call relationship id; preserve legacy shape when module_key has no '/'."""
    # Legacy packages use stem-only keys and the historical bare-name form.
    # Disambiguated keys (package-relative paths) embed the module key so two
    # util.c files cannot emit the same relationship id.
    if "/" in module_key:
        return f"rel:call:{module_key}:{caller}:{callee}:{line}:{col}"
    return f"rel:call:{caller}:{callee}:{line}:{col}"


def _disambiguate_duplicate_relationship_ids(
    relationships: List[Dict[str, Any]],
) -> None:
    """Keep legacy IDs when unique; add stable endpoint digests on collision."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for relationship in relationships:
        grouped.setdefault(str(relationship.get("id", "")), []).append(
            relationship
        )

    for base_id, rows in grouped.items():
        if len(rows) < 2:
            continue
        for relationship in rows:
            # Deliberately exclude source_file so IDs are checkout-path independent.
            material = "\0".join(
                str(relationship.get(field, ""))
                for field in ("type", "source", "target", "span", "extractor")
            )
            digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
            relationship["id"] = f"{base_id}:{digest}"

    remaining: Dict[str, int] = {}
    for relationship in relationships:
        rel_id = str(relationship.get("id", ""))
        remaining[rel_id] = remaining.get(rel_id, 0) + 1
    duplicates = sorted(
        rel_id for rel_id, count in remaining.items() if count > 1
    )
    if duplicates:
        raise ValueError(
            "C relationship IDs remain ambiguous after disambiguation: "
            f"{duplicates[:5]}"
        )


def _declarator_name(decl: Node) -> Optional[str]:
    """Find the function name identifier under a (possibly pointer) declarator."""
    stack = [decl]
    while stack:
        n = stack.pop(0)
        if n.type == "function_declarator":
            for c in n.children:
                if c.type == "identifier":
                    return c.text.decode()
                if c.type in ("parenthesized_declarator", "pointer_declarator"):
                    stack.append(c)
        elif n.type in ("pointer_declarator", "parenthesized_declarator"):
            stack.extend(n.children)
    return None


def _func_name(fn_def: Node) -> Optional[str]:
    for c in fn_def.children:
        if c.type in ("function_declarator", "pointer_declarator", "parenthesized_declarator"):
            name = _declarator_name(c)
            if name and name not in _C_KEYWORDS:
                return name
    return None


def _walk(node: Node):
    yield node
    for c in node.children:
        yield from _walk(c)


def _collect_functions(root: Node) -> List[Node]:
    return [n for n in _walk(root) if n.type == "function_definition"]


def _named_type(node: Node) -> Optional[str]:
    """type_identifier of a struct/enum specifier that actually has a body."""
    has_body = any(
        c.type in ("field_declaration_list", "enumerator_list") for c in node.children
    )
    if not has_body:
        return None
    for c in node.children:
        if c.type == "type_identifier":
            return c.text.decode()
    return None


def build_c_byog(package_dir: Path) -> Dict[str, List[Dict[str, Any]]]:
    package_dir = Path(package_dir).resolve()
    files = list_indexed_c_files(package_dir)
    module_keys = build_module_key_map(package_dir, files)
    parser = _parser()
    parsed = []
    defined_funcs: Dict[str, List[str]] = {}  # bare name -> [module_key:name, ...]

    # Pass 1: parse + collect package-wide function definitions.
    for path in files:
        src = path.read_bytes()
        tree = parser.parse(src)
        module_key = module_keys[path]
        for fn in _collect_functions(tree.root_node):
            name = _func_name(fn)
            if name:
                title = symbol_entity_title(module_key, name)
                defined_funcs.setdefault(name, [])
                if title not in defined_funcs[name]:
                    defined_funcs[name].append(title)
        parsed.append((path, src, tree, module_key))

    entities: List[Dict[str, Any]] = []
    relationships: List[Dict[str, Any]] = []
    text_units: List[Dict[str, Any]] = []
    observations: List[Dict[str, Any]] = []
    hid = 0

    def add_entity(
        title: str, etype: str, node: Node, src: bytes, path: Path, module_key: str
    ):
        nonlocal hid
        hid += 1
        # Entity id embeds the full title (not a lossy slug) so punctuation in
        # disambiguated module keys cannot collapse distinct entities.
        ent_id = f"ent:{etype}:{title}"
        # Text-unit id keeps the historical shape for stem-only keys. The
        # module_key prefix is not slugged, so path-disambiguated keys stay unique
        # even when _slug(title) would otherwise collide.
        tu_id = f"tu:{module_key}:{_slug(title)}"
        doc_id = f"doc:{module_key}"
        snippet = _text(src, node)
        entities.append({
            "id": ent_id,
            "title": title,
            "type": etype,
            "description": f"{etype} {title} defined in {path.name}",
            "snippet": snippet,
            "text_unit_ids": [tu_id],
            "human_readable_id": hid,
            "source_file": str(path),
            "span": _span(node),
            "extractor": "tree-sitter-c",
            "confidence": 1.0,
            "is_deterministic": True,
            "document_ids": [doc_id],
            "covariate_ids": [],
        })
        text_units.append({
            "id": tu_id,
            "human_readable_id": hid,
            "text": snippet,
            "n_tokens": max(1, len(snippet.split())),
            "document_id": doc_id,
            "document_ids": [doc_id],
            "entity_ids": [ent_id],
            "relationship_ids": [],
            "covariate_ids": [],
            "source_file": str(path),
            "span": _span(node),
            "extractor": "tree-sitter-c",
            "confidence": 1.0,
            "is_deterministic": True,
        })
        return ent_id

    rid = 0

    def add_contains(
        file_id: str, target_title: str, module_key: str, name: str
    ):
        nonlocal rid
        rid += 1
        relationships.append({
            "id": f"rel:contains:{module_key}:{name}",
            "source": file_id,
            "target": target_title,
            "type": "contains",
            "description": f"{module_key} contains {name}",
            "weight": 1.0,
            "text_unit_ids": [],
            "human_readable_id": rid,
            "source_file": "",
            "span": "",
            "extractor": "tree-sitter-c",
            "confidence": 1.0,
            "is_deterministic": True,
            "document_ids": [f"doc:{module_key}"],
            "covariate_ids": [],
        })

    # Pass 2: entities + calls per file.
    for path, src, tree, module_key in parsed:
        file_title = file_entity_title(path, module_key)
        file_id = add_entity(
            file_title, "file", tree.root_node, src, path, module_key
        )

        seen_titles: set[str] = set()
        for n in _walk(tree.root_node):
            title = etype = name = None
            if n.type == "function_definition":
                nm = _func_name(n)
                if nm:
                    name, etype, title = nm, "function", symbol_entity_title(module_key, nm)
            elif n.type == "struct_specifier":
                nm = _named_type(n)
                if nm:
                    name, etype, title = nm, "struct", symbol_entity_title(module_key, nm)
            elif n.type == "enum_specifier":
                nm = _named_type(n)
                if nm:
                    name, etype, title = nm, "enum", symbol_entity_title(module_key, nm)
            elif n.type == "type_definition":
                td = [c for c in n.children if c.type == "type_identifier"]
                if td:
                    nm = td[-1].text.decode()
                    name, etype, title = nm, "typedef", symbol_entity_title(module_key, nm)
            if title and title not in seen_titles:
                seen_titles.add(title)
                add_entity(title, etype, n, src, path, module_key)
                add_contains(file_id, title, module_key, name)

        # calls: attribute each call_expression to its enclosing function.
        for fn in _collect_functions(tree.root_node):
            caller = _func_name(fn)
            if not caller:
                continue
            caller_title = symbol_entity_title(module_key, caller)
            for n in _walk(fn):
                if n.type != "call_expression":
                    continue
                callee_node = n.child_by_field_name("function")
                if callee_node is None or callee_node.type != "identifier":
                    continue  # function-pointer / member calls: out of bootstrap scope
                callee = callee_node.text.decode()
                candidates = defined_funcs.get(callee, [])
                same_file_candidate = symbol_entity_title(module_key, callee)
                resolved_target = None
                if same_file_candidate in candidates:
                    resolved_target = same_file_candidate
                elif len(candidates) == 1:
                    resolved_target = candidates[0]

                if resolved_target is not None:
                    rid += 1
                    line = n.start_point[0] + 1
                    col = n.start_point[1]
                    relationships.append({
                        "id": _call_rel_id(module_key, caller, callee, line, col),
                        "source": caller_title,
                        "target": resolved_target,
                        "type": "calls",
                        "description": f"{caller} calls {callee} (C call)",
                        "weight": 0.9,
                        "text_unit_ids": [],
                        "human_readable_id": rid,
                        "source_file": str(path),
                        "span": f"{line}:{col}",
                        "extractor": "tree-sitter-c",
                        "confidence": 0.9,
                        "is_deterministic": True,
                        "document_ids": [f"doc:{module_key}"],
                        "covariate_ids": [],
                    })
                else:
                    reason = "ambiguous C call" if candidates else "external/undefined C call"
                    description_reason = "ambiguous" if candidates else "external/undefined"
                    observations.append({
                        "source": caller_title,
                        "display_target": callee,
                        "confidence": 0.4,
                        "reason": reason,
                        "source_file": str(path),
                        "span": f"{n.start_point[0] + 1}:{n.start_point[1]}",
                        "extractor": "tree-sitter-c",
                        "description": (
                            f"{caller} calls {callee} ({description_reason})"
                        ),
                    })

    _disambiguate_duplicate_relationship_ids(relationships)
    data = {
        "entities": entities,
        "relationships": relationships,
        "text_units": text_units,
        "call_observations": observations,
    }
    # Provenance labels only: mark facts that sit on preprocessor ground
    # tree-sitter cannot verify. Does not demote is_deterministic or drop edges
    # (audit pass rates stay comparable). See scripts/c_preprocessor.py.
    try:
        from c_preprocessor import annotate_byog  # type: ignore

        annotate_byog(data, package_dir)
    except Exception:
        # Annotation is best-effort; extraction must not fail if the diagnostic
        # module is unavailable. Tests import c_preprocessor directly.
        for e in entities:
            e.setdefault("preprocessor_dependent", False)
            e.setdefault("preprocessor_reasons", [])
        for r in relationships:
            r.setdefault("preprocessor_dependent", False)
            r.setdefault("preprocessor_reasons", [])
        for o in observations:
            o.setdefault("preprocessor_dependent", False)
            o.setdefault("preprocessor_reasons", [])
    return data
