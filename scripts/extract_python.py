#!/usr/bin/env python
"""
Minimal tree-sitter based Python extractor (Phase 0 prototype).

Walks a Python file and emits entity/relationship records with full provenance.

This is the foundation for turning source into the GraphRAG BYOG parquets
(entities.parquet, relationships.parquet, text_units.parquet).

Current scope (deliberately small):
- file entity
- function / class entities (top level)
- top-level data/constant entities (module-level assignments)
- contains edges (file -> symbol)
- import edges (rough)
- conservative "calls" (name-based resolution inside the same file)
- conservative "uses_data" edges from functions/methods to module-level data

Does NOT replace semantic analysis (no Jedi, mypy, full control flow yet).

Usage example:
    uv run python scripts/extract_python.py examples/mini_game/sim.py
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from tree_sitter import Language, Parser, Node  # type: ignore
import tree_sitter_python as tspython  # type: ignore

# Load the Python language
PY_LANGUAGE = Language(tspython.language())
parser = Parser(PY_LANGUAGE)


def get_text(source_bytes: bytes, node: Node) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def make_id(kind: str, name: str, source_file: str) -> str:
    safe = name.replace(".", "_")
    return f"ent:{kind}:{Path(source_file).stem}:{safe}"


def extract_from_file(path: Path, use_advanced: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    source = path.read_bytes()
    source_text = source.decode("utf-8", errors="replace")
    tree = parser.parse(source)
    root = tree.root_node

    entities: List[Dict[str, Any]] = []
    relationships: List[Dict[str, Any]] = []

    source_file = str(path)

    # File entity (always)
    file_id = f"ent:file:{path.name}"
    entities.append(
        {
            "id": file_id,
            "title": path.name,
            "type": "file",
            "description": f"Python source file: {path}",
            "text_unit_ids": [f"tu:file:{path.name}"],
            "human_readable_id": len(entities) + 1,
            "source_file": source_file,
            "span": f"1:0-{len(source.splitlines())}:0",
            "extractor": "tree-sitter-python",
            "confidence": 1.0,
            "is_deterministic": True,
        }
    )

    # Collect top-level defs (including @dataclass etc. which are decorated_definition)
    defined_names: List[str] = []
    defined_kinds: Dict[str, str] = {}
    defined_methods: List[str] = []

    def ast_span(node: ast.AST) -> str:
        lineno = getattr(node, "lineno", 1)
        col = getattr(node, "col_offset", 0)
        end_lineno = getattr(node, "end_lineno", lineno)
        end_col = getattr(node, "end_col_offset", col)
        return f"{lineno}:{col}-{end_lineno}:{end_col}"

    def ast_snippet(node: ast.AST) -> str:
        return ast.get_source_segment(source_text, node) or ""

    def emit_class_members(body_node: Node | None, class_qual: str, class_ent_id: str) -> None:
        """Emit method (and nested-class) entities for a class body, recursively.

        Nested classes (class inside class) get dotted titles like
        ``Owner.Nested`` / ``Owner.Nested.method`` so callers and observations
        carry clean titles instead of raw ``ent:fn:*`` ids.
        """
        if body_node is None:
            return
        for member in body_node.named_children:
            inner = member
            if member.type == "decorated_definition":
                defn = member.child_by_field_name("definition")
                if defn is None or defn.type not in ("function_definition", "class_definition"):
                    continue
                inner = defn
            if inner.type not in ("function_definition", "class_definition"):
                continue
            mname_node = inner.child_by_field_name("name")
            if mname_node is None:
                continue
            mname = get_text(source, mname_node)
            qualified = f"{class_qual}.{mname}"
            span_node = member if member.type == "decorated_definition" else inner
            member_kind = "method" if inner.type == "function_definition" else "class"
            member_id = make_id(member_kind, qualified, source_file)

            entities.append(
                {
                    "id": member_id,
                    "title": qualified,
                    "type": member_kind,
                    "description": f"{member_kind} {qualified} defined in {path.name}",
                    "snippet": get_text(source, span_node),
                    "text_unit_ids": [f"tu:file:{path.name}"],
                    "human_readable_id": len(entities) + 1,
                    "source_file": source_file,
                    "span": f"{span_node.start_point[0]+1}:{span_node.start_point[1]}-{span_node.end_point[0]+1}:{span_node.end_point[1]}",
                    "extractor": "tree-sitter-python",
                    "confidence": 1.0,
                    "is_deterministic": True,
                }
            )
            relationships.append(
                {
                    "id": f"rel:contains:{path.name}:{qualified}",
                    "source": class_ent_id,
                    # methods point at the title (legacy), classes at the id.
                    "target": qualified if member_kind == "method" else member_id,
                    "type": "contains",
                    "description": f"{class_qual} contains {member_kind} {mname}",
                    "weight": 1.0,
                    "text_unit_ids": [f"tu:file:{path.name}"],
                    "human_readable_id": len(relationships) + 1,
                    "source_file": source_file,
                    "span": "",
                    "extractor": "tree-sitter-python",
                    "confidence": 1.0,
                    "is_deterministic": True,
                }
            )
            if member_kind == "method":
                defined_methods.append(qualified)
            else:
                emit_class_members(inner.child_by_field_name("body"), qualified, member_id)

    for child in root.children:
        node = child
        if child.type == "decorated_definition":
            defn = child.child_by_field_name("definition")
            if defn is not None and defn.type in ("function_definition", "class_definition"):
                node = defn
            else:
                continue
        if node.type in ("function_definition", "class_definition"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            name = get_text(source, name_node)
            kind = "fn" if node.type == "function_definition" else "class"
            ent_id = make_id(kind, name, source_file)

            doc = ""
            # crude docstring extraction
            body = node.child_by_field_name("body")
            if body and body.named_child_count > 0:
                first = body.named_children[0]
                if first.type == "expression_statement":
                    expr = first.named_children[0] if first.named_child_count else None
                    if expr and expr.type == "string":
                        doc = get_text(source, expr).strip('\'" \n')

            # Use outer decorated node for full span/snippet (includes the @dataclass decorator)
            span_node = child if child.type == "decorated_definition" else node
            snippet = get_text(source, span_node)

            entities.append(
                {
                    "id": ent_id,
                    "title": name,
                    "type": kind,
                    "description": doc or f"{kind} {name} defined in {path.name}",
                    "snippet": snippet,
                    "text_unit_ids": [f"tu:file:{path.name}"],
                    "human_readable_id": len(entities) + 1,
                    "source_file": source_file,
                    "span": f"{span_node.start_point[0]+1}:{span_node.start_point[1]}-{span_node.end_point[0]+1}:{span_node.end_point[1]}",
                    "extractor": "tree-sitter-python",
                    "confidence": 1.0,
                    "is_deterministic": True,
                }
            )
            defined_names.append(name)
            defined_kinds[name] = kind

            # contains edge (point to the symbol)
            relationships.append(
                {
                    "id": f"rel:contains:{path.name}:{name}",
                    "source": file_id,
                    "target": ent_id,
                    "type": "contains",
                    "description": f"{path.name} contains {kind} {name}",
                    "weight": 1.0,
                    "text_unit_ids": [f"tu:file:{path.name}"],
                    "human_readable_id": len(relationships) + 1,
                    "source_file": source_file,
                    "span": "",
                    "extractor": "tree-sitter-python",
                    "confidence": 1.0,
                    "is_deterministic": True,
                }
            )

            if kind == "class" and body:
                emit_class_members(body, name, ent_id)

    # Module-level data/constant entities. These are essential for porting
    # table-driven code such as sqlparse.keywords.SQL_REGEX / KEYWORDS_*:
    # call-closure alone finds the functions, but not the data tables they read.
    module_data_names: List[str] = []
    try:
        ast_tree_for_data = ast.parse(source)
    except Exception:
        ast_tree_for_data = None
    if ast_tree_for_data is not None:
        for stmt in ast_tree_for_data.body:
            targets: List[ast.AST] = []
            if isinstance(stmt, ast.Assign):
                targets = list(stmt.targets)
            elif isinstance(stmt, ast.AnnAssign):
                targets = [stmt.target]
            else:
                continue
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                name = target.id
                if name in defined_names:
                    continue
                if name.startswith("__") and name.endswith("__"):
                    continue
                ent_id = make_id("data", name, source_file)
                snippet = ast_snippet(stmt)
                entities.append(
                    {
                        "id": ent_id,
                        "title": name,
                        "type": "data",
                        "description": f"module-level data {name} defined in {path.name}",
                        "snippet": snippet,
                        "text_unit_ids": [f"tu:file:{path.name}"],
                        "human_readable_id": len(entities) + 1,
                        "source_file": source_file,
                        "span": ast_span(stmt),
                        "extractor": "python-ast",
                        "confidence": 1.0,
                        "is_deterministic": True,
                    }
                )
                module_data_names.append(name)

    # Structured imports (for cross-file resolution in bridge)
    imports: List[Dict[str, Any]] = []
    for child in root.children:
        if child.type in ("import_statement", "import_from_statement"):
            text = get_text(source, child)
            module_name = ""
            imported_names: List[str] = []
            is_relative = False

            # Try to extract module and names from tree-sitter structure
            module_node = child.child_by_field_name("module")
            if module_node:
                module_name = get_text(source, module_node).lstrip(".")
            if child.type == "import_from_statement":
                # names are usually under "name" or children
                for c in child.children:
                    if c.type == "relative_import":
                        is_relative = True
                    if c.type == "dotted_name" or c.type == "identifier":
                        nm = get_text(source, c).lstrip(".")
                        if nm and nm != module_name and nm not in imported_names:
                            imported_names.append(nm)
                    if c.type == "aliased_import":
                        # handle "foo as bar"
                        for gc in c.children:
                            if gc.type in ("identifier", "dotted_name"):
                                nm = get_text(source, gc).lstrip(".")
                                if nm and nm not in imported_names:
                                    imported_names.append(nm)
            else:
                # plain import foo, bar
                for c in child.children:
                    if c.type in ("dotted_name", "identifier"):
                        nm = get_text(source, c).lstrip(".")
                        if nm and nm not in imported_names:
                            imported_names.append(nm)

            if text.startswith("from .") or module_name.startswith("."):
                is_relative = True

            imports.append({
                "module": module_name or text,
                "names": imported_names or [text],
                "is_relative": is_relative,
                "text": text,
            })

            # Keep a (rough) relationship for now; bridge will create better module-module ones
            relationships.append(
                {
                    "id": f"rel:import:{path.name}:{len(relationships)}",
                    "source": file_id,
                    "target": f"ent:module:{(module_name or text)[:40]}",
                    "type": "imports",
                    "description": text,
                    "weight": 0.5,
                    "text_unit_ids": [f"tu:file:{path.name}"],
                    "human_readable_id": len(relationships) + 1,
                    "source_file": source_file,
                    "span": f"{child.start_point[0]+1}",
                    "extractor": "tree-sitter-python",
                    "confidence": 0.8,
                    "is_deterministic": True,
                }
            )

    imported_call_names = {
        name.split(".")[-1]
        for imp in imports
        for name in imp.get("names", [])
        if name and " import " not in name
    }
    callable_names = set(defined_names) | imported_call_names
    known_callers = set(defined_names) | set(defined_methods)

    def enclosing_callable_title(node: Node) -> str:
        """Return the entity title for the nearest enclosing function/method."""
        cur = node
        while cur:
            if cur.type == "function_definition":
                nm = cur.child_by_field_name("name")
                if nm is None:
                    return "unknown"
                fn_name = get_text(source, nm)
                parent = cur.parent
                while parent:
                    if parent.type == "class_definition":
                        class_name_node = parent.child_by_field_name("name")
                        if class_name_node is not None:
                            return f"{get_text(source, class_name_node)}.{fn_name}"
                        break
                    parent = parent.parent
                return fn_name
            cur = cur.parent
        return "unknown"

    # Conservative calls: local definitions and explicitly imported names only.
    # This is syntax only - real version will need name resolution.
    def walk_calls(node: Node):
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func and func.type == "identifier":
                callee = get_text(source, func)
                if callee in callable_names:
                    caller = enclosing_callable_title(node)

                    if caller != "unknown" and caller in known_callers:
                        is_local = callee in defined_names
                        callee_target = (
                            make_id(defined_kinds.get(callee, "fn"), callee, source_file)
                            if is_local
                            else callee
                        )
                        caller_kind = "method" if "." in caller else "fn"
                        relationships.append(
                            {
                                "id": f"rel:call:{caller}:{callee}:{node.start_point[0]}",
                                "source": make_id(caller_kind, caller, source_file),
                                "target": callee_target,
                                "type": "calls",
                                "description": f"{caller} may call {callee} (syntax only, {'local name match' if is_local else 'imported name match'})",
                                "weight": 0.75 if is_local else 0.65,
                                "text_unit_ids": [f"tu:file:{path.name}"],
                                "human_readable_id": len(relationships) + 1,
                                "source_file": source_file,
                                "span": f"{node.start_point[0]+1}:{node.start_point[1]}",
                                "extractor": "tree-sitter-python",
                                "confidence": 0.75 if is_local else 0.65,
                                "is_deterministic": False,  # name match only, no resolution
                            }
                        )
        for c in node.children:
            walk_calls(c)

    walk_calls(root)

    # Module entity for the file. If a file is named main.py and also defines
    # def main(), keep module and function titles distinct after BYOG prefixing.
    path_stem = Path(path).stem
    module_title = "__module__" if path_stem in defined_names else path_stem
    module_id = f"ent:module:{module_title}"
    entities.append({
        "id": module_id,
        "title": module_title,
        "type": "module",
        "description": f"Python module {path_stem} (from {path.name})",
        "text_unit_ids": [f"tu:file:{path.name}"],
        "human_readable_id": len(entities) + 1,
        "source_file": source_file,
        "span": "module",
        "extractor": "tree-sitter-python",
        "confidence": 1.0,
        "is_deterministic": True,
    })
    # file contains module (lightweight)
    relationships.append({
        "id": f"rel:contains-module:{path.name}",
        "source": file_id,
        "target": module_id,
        "type": "contains",
        "description": f"{path.name} defines module {path_stem}",
        "weight": 1.0,
        "text_unit_ids": [f"tu:file:{path.name}"],
        "human_readable_id": len(relationships) + 1,
        "source_file": source_file,
        "span": "",
        "extractor": "tree-sitter-python",
        "confidence": 1.0,
        "is_deterministic": True,
    })

    for data_name in module_data_names:
        relationships.append(
            {
                "id": f"rel:contains-data:{path.name}:{data_name}",
                "source": module_id,
                "target": make_id("data", data_name, source_file),
                "type": "contains",
                "description": f"module {path_stem} defines data {data_name}",
                "weight": 1.0,
                "text_unit_ids": [f"tu:file:{path.name}"],
                "human_readable_id": len(relationships) + 1,
                "source_file": source_file,
                "span": "",
                "extractor": "python-ast",
                "confidence": 1.0,
                "is_deterministic": True,
            }
        )

    _enhance_with_ast(source, path, entities, relationships, defined_names)

    if use_advanced:
        for rel in _try_jedi_adapter(source, path) + _try_pyright_adapter(path):
            rel.setdefault("id", f"rel:advanced:{path.name}:{len(relationships) + 1}")
            rel.setdefault("source_file", source_file)
            rel.setdefault("span", "")
            rel.setdefault("text_unit_ids", [f"tu:file:{path.name}"])
            rel.setdefault("human_readable_id", len(relationships) + 1)
            rel.setdefault("extractor", "advanced-resolver")
            rel.setdefault("confidence", 0.90)
            rel.setdefault("is_deterministic", False)
            relationships.append(rel)

    return {
        "entities": entities,
        "relationships": relationships,
        "imports": imports,
        "module_title": module_title,
    }


def _enhance_with_ast(source: bytes, path: Path, entities: List[Dict], relationships: List[Dict], defined_names: List[str]) -> None:
    """Use stdlib ast to add deterministic import hints to tree-sitter call edges.

    This is still intentionally conservative: AST direct imports can strengthen
    a relationship with a resolved_target_hint, while future Jedi/Pyright passes
    can add richer reference/type information behind an optional try/fallback.
    """
    try:
        tree = ast.parse(source)
    except Exception:
        return

    import_map: Dict[str, str] = {}  # local_name -> module (e.g. "update_player" -> "physics")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local = alias.asname or alias.name
                if node.module:
                    mod = ('.' * node.level + node.module) if node.level else node.module
                else:
                    # from . import foo   or from . import foo as bar
                    mod = ('.' * node.level + alias.name) if node.level else alias.name
                import_map[local] = mod.lstrip(".")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                import_map[local] = alias.name
                import_map[alias.name] = alias.name

    def _collect_qualified_functions(node: ast.AST, prefix: str = "") -> List[tuple[str, int, int]]:
        """Collect (qualified_name, lineno, end_lineno) for all functions, respecting nesting.
        Qualified names use dots for nesting (e.g. 'outer.inner', 'Demo.run').
        """
        spans: List[tuple[str, int, int]] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qname = f"{prefix}.{node.name}" if prefix else node.name
            end = getattr(node, "end_lineno", node.lineno)
            spans.append((qname, node.lineno, end))
            for child in node.body:
                spans.extend(_collect_qualified_functions(child, qname))
        elif isinstance(node, ast.ClassDef):
            cprefix = f"{prefix}.{node.name}" if prefix else node.name
            for child in node.body:
                spans.extend(_collect_qualified_functions(child, cprefix))
        else:
            for child in ast.iter_child_nodes(node):
                spans.extend(_collect_qualified_functions(child, prefix))
        return spans

    function_spans: List[tuple[str, int, int]] = _collect_qualified_functions(tree)

    def enclosing_function_name(call_node: ast.AST) -> str:
        lineno = getattr(call_node, "lineno", -1)
        matches = [
            (start, end, name)
            for name, start, end in function_spans
            if start <= lineno <= end
        ]
        if not matches:
            return "unknown"
        matches.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        return matches[0][2]

    def get_dotted_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = get_dotted_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return None

    def module_title(module_path: str) -> str:
        return module_path.split(".")[-1] if module_path else Path(path).stem

    def imported_callable_hint(name: str) -> tuple[str, str] | None:
        module = import_map.get(name)
        if module:
            return f"{module_title(module)}:{name}", f"{module}.{name}"
        if name in defined_names:
            return f"{Path(path).stem}:{name}", name
        return None

    def module_attr_hint(base_expr: str, attr: str) -> tuple[str, str]:
        module_path = import_map.get(base_expr)
        if not module_path:
            parts = base_expr.split(".")
            root = parts[0]
            if root in import_map:
                root_target = import_map[root]
                rest = parts[1:]
                module_path = ".".join([root_target] + rest) if rest else root_target
            else:
                module_path = base_expr
        return f"{module_title(module_path)}:{attr}", f"{module_path}.{attr}"

    def imported_module_attr_hint(base_expr: str, attr: str) -> tuple[str, str] | None:
        """Resolve module.func only when the receiver is known to be an import.

        Unknown attribute receivers such as regex.match(), obj.match(), or
        clause.match() are method/dynamic calls until proven otherwise. Binding
        them to a same-named module function would create false ground-truth
        CALLS edges.
        """
        parts = base_expr.split(".")
        if base_expr in import_map or (parts and parts[0] in import_map):
            return module_attr_hint(base_expr, attr)
        return None

    def constructor_type_hint(constructor: str) -> str | None:
        if "." in constructor:
            base_expr, attr = constructor.rsplit(".", 1)
            # Factory classmethod on a same-file class: Class.from_x(...) -> Class
            # (only when the classmethod actually returns cls(...)/Class(...)).
            if (base_expr, attr) in factory_methods:
                return f"{Path(path).stem}:{base_expr}"
            hint, _ = module_attr_hint(base_expr, attr)
            return hint
        # Same-file class constructor: LocalClass(...) -> LocalClass.
        if constructor in local_classes:
            return f"{Path(path).stem}:{constructor}"
        imported_hint = imported_callable_hint(constructor)
        if imported_hint:
            return imported_hint[0]
        return None

    def get_type_from_annotation(ann: ast.AST | None) -> str | None:
        """Return a type marker from annotation.

        Supports:
        - bare/qualified: Demo, pkg.Demo -> "Demo" or "pkg.Demo" (later resolved via constructor_type_hint)
        - containers: list[T], List[T], typing.List[T], collections.abc.Sequence[T] -> "container:list"
        - unions: Optional[Demo], Demo | None, Union[Demo, None], Demo | Other -> primary non-None type (or None if ambiguous multiple classes)
        This gives honest hints on real code that uses typing aliases and PEP 604 unions.
        """
        if ann is None:
            return None

        def is_none_marker(marker: str | None) -> bool:
            return marker is not None and str(marker).lower() in {"none", "nonetype"}

        def single_union_type(candidates: List[str]) -> str | None:
            real = []
            for candidate in candidates:
                if candidate and not is_none_marker(candidate):
                    real.append(candidate)
            unique = list(dict.fromkeys(real))
            if len(unique) == 1:
                return unique[0]
            if len(unique) > 1:
                return "ambiguous:annotation"
            return None

        def container_marker(base_name: str | None) -> str | None:
            if not base_name:
                return None
            base = base_name.lower()
            simple = base.rsplit(".", 1)[-1]
            if simple in ("list", "dict", "set", "tuple"):
                return f"container:{simple}"
            if simple in ("sequence", "iterable", "mutablesequence"):
                return "container:list"
            return None

        # PEP 604 unions: Demo | None
        if isinstance(ann, ast.BinOp) and isinstance(getattr(ann, "op", None), ast.BitOr):
            def collect_union_parts(node: ast.AST) -> List[str]:
                if isinstance(node, ast.BinOp) and isinstance(getattr(node, "op", None), ast.BitOr):
                    return collect_union_parts(node.left) + collect_union_parts(node.right)
                marker = get_type_from_annotation(node)
                return [marker] if marker else []

            return single_union_type(collect_union_parts(ann))

        if isinstance(ann, ast.Constant) and ann.value is None:
            return "None"

        if isinstance(ann, ast.Name):
            name = ann.id
            marker = container_marker(name)
            if marker:
                return marker
            return name

        if isinstance(ann, ast.Attribute):
            return get_dotted_name(ann)

        if isinstance(ann, ast.Subscript):
            val = ann.value
            base = None
            if isinstance(val, ast.Name):
                base = val.id.lower()
            elif isinstance(val, ast.Attribute):
                dotted = get_dotted_name(val)
                base = dotted.lower() if dotted else None

            # typing.List, List, collections.abc.Sequence etc. → container
            marker = container_marker(base)
            if marker:
                return marker

            # Optional[T], Union[T, ...] → unwrap to primary type
            if base and (base in ("optional", "union") or base.endswith(".optional") or base.endswith(".union")):
                slice_node = ann.slice
                candidates = []
                if isinstance(slice_node, ast.Tuple):
                    for elt in getattr(slice_node, "elts", []):
                        t = get_type_from_annotation(elt)
                        if t:
                            candidates.append(t)
                else:
                    t = get_type_from_annotation(slice_node)
                    if t:
                        candidates.append(t)
                # Multiple real types or only None → no single useful type for hint (honesty).
                return single_union_type(candidates)

            if isinstance(val, (ast.Name, ast.Attribute)):
                return get_dotted_name(val)
            return None

        return None

    class_for_method: Dict[str, str] = {}  # qualified (or bare) method name -> ClassName for self resolution within file

    def _index_class_methods(node: ast.AST, prefix: str = "") -> None:
        if isinstance(node, ast.ClassDef):
            cqual = f"{prefix}.{node.name}" if prefix else node.name
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Value is the (possibly nested-qualified) class name, so self/cls
                    # hints and caller titles match the extracted entity titles.
                    class_for_method[f"{cqual}.{item.name}"] = cqual
                    class_for_method.setdefault(item.name, cqual)  # bare-name compat (outer wins)
                else:
                    _index_class_methods(item, cqual)
        else:
            for child in ast.iter_child_nodes(node):
                _index_class_methods(child, prefix)

    _index_class_methods(tree)

    # Method names per (simple) class name, for resolving KnownClass.method()
    # calls made via the class name itself (e.g. classmethod Version.parse(...)).
    class_methods: Dict[str, set[str]] = defaultdict(set)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    class_methods[node.name].add(item.name)

    # Local classes and factory classmethods, for resolving constructor types of
    # same-file class instances (link 1). A factory is a @classmethod whose body
    # returns `cls(...)` or `Class(...)` -- an alternative constructor that yields
    # an instance of its own class (e.g. JsonPatch.from_string).
    local_classes: set[str] = set(class_methods.keys())
    factory_methods: set[tuple[str, str]] = set()
    property_methods: set[tuple[str, str]] = set()  # (Class, name) decorated @property
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decos = item.decorator_list
            if any(isinstance(d, ast.Name) and d.id in ("property", "cached_property") for d in decos):
                property_methods.add((node.name, item.name))
            if not any(isinstance(d, ast.Name) and d.id == "classmethod" for d in decos):
                continue
            for sub in ast.walk(item):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Call):
                    rfn = sub.value.func
                    if isinstance(rfn, ast.Name) and rfn.id in ("cls", node.name):
                        factory_methods.add((node.name, item.name))
                        break

    # Conservative data-dependency edges. These intentionally model reads of
    # module-level tables/constants separately from call edges so a porting
    # context pack can include e.g. sqlparse.keywords.SQL_REGEX and KEYWORDS_*
    # when packing lexer initialization.
    data_names = {
        str(e.get("title"))
        for e in entities
        if str(e.get("type", "")).lower() == "data"
    }
    seen_data_edges: set[tuple[str, str, int]] = set()

    def emit_uses_data(call_node: ast.AST, target: str, description: str) -> None:
        caller = enclosing_function_name(call_node)
        if caller == "unknown":
            return
        caller_kind = "method" if caller in class_for_method else "fn"
        source = make_id(caller_kind, caller, str(path))
        key = (source, target, getattr(call_node, "lineno", 0))
        if key in seen_data_edges:
            return
        seen_data_edges.add(key)
        relationships.append(
            {
                "id": f"rel:uses-data:{caller}:{target}:{getattr(call_node, 'lineno', 0)}:{getattr(call_node, 'col_offset', 0)}",
                "source": source,
                "target": target,
                "type": "uses_data",
                "description": description,
                "weight": 0.90,
                "text_unit_ids": [f"tu:file:{path.name}"],
                "human_readable_id": len(relationships) + 1,
                "source_file": str(path),
                "span": f"{getattr(call_node, 'lineno', 0)}:{getattr(call_node, 'col_offset', 0)}",
                "extractor": "python-ast",
                "confidence": 0.90,
                "is_deterministic": True,
            }
        )

    def looks_like_module_constant(name: str) -> bool:
        return name.isupper() or name.startswith(("KEYWORDS", "SQL_REGEX"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in data_names:
            emit_uses_data(
                node,
                make_id("data", node.id, str(path)),
                f"{enclosing_function_name(node)} reads module data {node.id}",
            )
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            dotted = get_dotted_name(node)
            if not dotted or "." not in dotted or not looks_like_module_constant(node.attr):
                continue
            base_expr, attr = dotted.rsplit(".", 1)
            if imported_module_attr_hint(base_expr, attr):
                hint, resolved_display = module_attr_hint(base_expr, attr)
                emit_uses_data(
                    node,
                    hint,
                    f"{enclosing_function_name(node)} reads imported module data {dotted} -> {resolved_display}",
                )

    # Collect assign events with lineno for reassignment guards + ambiguity tiers.
    # Use the *actual enclosing function* (qualified) for the assignment node (via lineno).
    # Multiple distinct constructors for the same var (if branches, rebinds between
    # classes with overlapping methods, alias shadowing to different types) will later
    # cause confidence downgrade instead of blindly picking a target.
    assign_events: Dict[str, List[tuple[int, str, str | None]]] = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: List[ast.AST] = []
            value: ast.AST | None = None
            annotation: ast.AST | None = None
            lineno = getattr(node, "lineno", 0)
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
                annotation = node.annotation
            if value is None and annotation is None:
                continue
            type_from_annot = get_type_from_annotation(annotation)
            # Detect constructor calls *and* builtin container literals (list/dict/set and their ctors).
            # Annotations (x: Demo, items: list[Event]) provide additional static type info
            # so that method calls can get honest hints even without a constructor expression
            # in the same scope, or to reinforce container classification.
            container_kind: str | None = None
            if value is not None:
                if isinstance(value, ast.List):
                    container_kind = "list"
                elif isinstance(value, ast.Dict):
                    container_kind = "dict"
                elif isinstance(value, ast.Set):
                    container_kind = "set"
                elif isinstance(value, ast.Call):
                    ctor_name = get_dotted_name(value.func) or ""
                    if ctor_name.lower() in ("list", "dict", "set"):
                        container_kind = ctor_name.lower()
            is_constructor = isinstance(value, ast.Call) and container_kind is None
            for target in targets:
                if isinstance(target, ast.Name):
                    var = target.id
                    enclosing = enclosing_function_name(node)
                    if enclosing == "unknown":
                        continue
                    effective: str | None = None
                    if container_kind:
                        effective = f"container:{container_kind}"
                    elif is_constructor:
                        constructor = get_dotted_name(value.func)
                        if constructor:
                            effective = constructor_type_hint(constructor)
                    contradicts_annotation = value is not None and isinstance(value, ast.Constant)
                    if effective is None and type_from_annot and not contradicts_annotation:
                        # annotation provides the type (bare "x: Demo" or unresolved call result).
                        if type_from_annot.startswith("container:"):
                            effective = type_from_annot
                        else:
                            effective = constructor_type_hint(type_from_annot)
                            if effective is None:
                                effective = type_from_annot
                    if effective:
                        assign_events[enclosing].append((lineno, var, effective))
                    elif value is not None:
                        # non-ctor value with no annot type info
                        assign_events[enclosing].append((lineno, var, None))

    # self/cls resolution using class_for_method. Emit bridge-resolvable method
    # titles so these edges survive the two-pass FQN normalization.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            base_dotted = get_dotted_name(base) or ""
            root_base = base_dotted.split(".", 1)[0] if base_dotted else ""
            if root_base in ("self", "cls"):
                attr = node.func.attr
                caller = enclosing_function_name(node)
                if caller in class_for_method:
                    class_name = class_for_method[caller]
                    simple_class = class_name.split(".")[-1]
                    # Direct self.method()/cls.method() is strong. Chained
                    # self.foo.method()/cls.foo.method() is still useful for
                    # singleton/cache patterns (sqlparse's
                    # cls._default_instance.default_initialization()), but only
                    # promote it if the called attr is actually a method on the
                    # same class.
                    is_direct = base_dotted in ("self", "cls")
                    is_known_same_class_method = attr in class_methods.get(simple_class, set())
                    if is_direct or is_known_same_class_method:
                        method_bare = caller.split(".")[-1] if "." in caller else caller
                        source_title = f"{Path(path).stem}:{class_name}.{method_bare}"
                        hint = f"{Path(path).stem}:{class_name}.{attr}"
                        relationships.append(
                            {
                                "id": f"rel:call:{class_name}.{method_bare}:{base_dotted}.{attr}:{getattr(node, 'lineno', 0)}",
                                "source": source_title,
                                "target": hint,
                                "type": "calls",
                                "description": f"{class_name}.{method_bare} calls {base_dotted}.{attr} (self/cls method in {class_name})",
                                "weight": 0.80,
                                "text_unit_ids": [f"tu:file:{path.name}"],
                                "human_readable_id": len(relationships) + 1,
                                "source_file": str(path),
                                "span": f"{getattr(node, 'lineno', 0)}",
                                "extractor": "tree-sitter-python+ast",
                                "confidence": 0.80,
                                "is_deterministic": True,
                                "resolved_target_hint": hint,
                            }
                        )

    # Property bridge: a method reading self.<name>/cls.<name> where <name> is an
    # @property on the same class is, semantically, a call to the property getter.
    # Emit a distinct `property` edge (NOT `calls`) so the closure can cross
    # property reads without inflating call counts or audit precision. Only Load
    # reads of real @property members of the enclosing class qualify.
    seen_property_edges: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load)):
            continue
        base = node.value
        if not (isinstance(base, ast.Name) and base.id in ("self", "cls")):
            continue
        caller = enclosing_function_name(node)
        if caller not in class_for_method:
            continue
        class_name = class_for_method[caller]
        simple_class = class_name.split(".")[-1]
        if (simple_class, node.attr) not in property_methods:
            continue
        method_bare = caller.split(".")[-1] if "." in caller else caller
        source_title = f"{Path(path).stem}:{class_name}.{method_bare}"
        hint = f"{Path(path).stem}:{class_name}.{node.attr}"
        dedupe = (source_title, hint)
        if dedupe in seen_property_edges:
            continue
        seen_property_edges.add(dedupe)
        relationships.append(
            {
                "id": f"rel:property:{class_name}.{method_bare}:{node.attr}:{getattr(node, 'lineno', 0)}",
                "source": source_title,
                "target": hint,
                "type": "property",
                "description": f"{class_name}.{method_bare} reads @property {class_name}.{node.attr}",
                "weight": 0.85,
                "text_unit_ids": [f"tu:file:{path.name}"],
                "human_readable_id": len(relationships) + 1,
                "source_file": str(path),
                "span": f"{getattr(node, 'lineno', 0)}",
                "extractor": "tree-sitter-python+ast",
                "confidence": 0.85,
                "is_deterministic": True,
                "resolved_target_hint": hint,
            }
        )

    # Improve existing tree-sitter calls with direct import hints. This keeps row
    # counts stable while giving the bridge better targets for cross-file calls.
    for rel in relationships:
        if rel.get("type") != "calls":
            continue
        raw_target = str(rel.get("target", ""))
        bare = raw_target.split(":")[-1].split(".")[-1]
        module = import_map.get(bare)
        if module:
            module_stem = module.split(".")[-1]
            rel["resolved_target_hint"] = f"{module_stem}:{bare}"
            rel["description"] = f"{rel.get('description', '')} (ast import hint: {module}.{bare})"
            rel["confidence"] = max(float(rel.get("confidence", 0.0) or 0.0), 0.85)
            rel["weight"] = max(float(rel.get("weight", 0.0) or 0.0), 0.85)
            rel["extractor"] = "tree-sitter-python+ast"
            rel["is_deterministic"] = True

    # Create concrete call relationships for Attribute cases (module.func,
    # module.submodule.func, and simple constructor-tracked method calls) that
    # the tree-sitter Name-only detector misses.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            dotted = get_dotted_name(func)
            if isinstance(func, ast.Attribute) and dotted and "." in dotted:
                base_expr, attr = dotted.rsplit(".", 1)
                if base_expr.split(".", 1)[0] in ("self", "cls"):
                    continue
                caller = enclosing_function_name(node)
                if caller == "unknown":
                    continue

                # Reassignment guard + ambiguity/confidence tiers lookup (next layer after qualified scopes).
                # - container literal (trace = []; trace.append) → 0.40 "builtin container list", distinct reason
                # - last relevant assign is non-ctor (string etc) → 0.40 weak, no hint (guarded by reassignment)
                # - multiple distinct ctors ... → 0.50 ambiguous ...
                # - single tracked ctor → 0.80 + specific hint
                # - else fallback (0.80)
                has_var_event = False
                has_none_event = False
                last_type_for_var: str | None = None
                distinct_ctors: set[str] = set()
                events = sorted(assign_events.get(caller, []), key=lambda x: x[0])
                call_lineno = getattr(node, "lineno", 0)
                for ev_l, ev_v, ev_t in events:
                    if ev_v == base_expr and ev_l <= call_lineno:
                        has_var_event = True
                        last_type_for_var = ev_t
                        if ev_t is None:
                            has_none_event = True
                        elif (
                            not str(ev_t).startswith("container:")
                            and not str(ev_t).startswith("ambiguous:")
                        ):
                            distinct_ctors.add(ev_t)
                if has_var_event and last_type_for_var and str(last_type_for_var).startswith("container:"):
                    kind = str(last_type_for_var).split(":", 1)[1]
                    container_methods = {"append", "extend", "insert", "pop", "remove", "clear", "add", "discard", "update", "get", "setdefault", "keys", "values", "items"}
                    if attr in container_methods:
                        # Distinct from plain "guarded by reassignment" (e.g. d="bad").
                        # This is a call on a locally-created container (trace=[] ; trace.append).
                        hint = None
                        resolved_display = f"{base_expr}.{attr} (builtin container {kind})"
                        confidence = 0.40
                        deterministic = False
                    else:
                        hint = None
                        resolved_display = f"{base_expr}.{attr} (guarded by reassignment)"
                        confidence = 0.40
                        deterministic = False
                elif has_var_event and last_type_for_var and str(last_type_for_var).startswith("ambiguous:"):
                    hint = None
                    resolved_display = f"{base_expr}.{attr} (ambiguous annotation)"
                    confidence = 0.50
                    deterministic = False
                elif has_var_event and last_type_for_var is None:
                    # guarded by a non-constructor reassignment (latest action)
                    hint = None
                    resolved_display = f"{base_expr}.{attr} (guarded by reassignment)"
                    confidence = 0.40
                    deterministic = False
                elif has_var_event and len(distinct_ctors) > 1:
                    # ambiguity tier: >1 known constructor types for the receiver in scope history
                    hint = None
                    resolved_display = f"{base_expr}.{attr} (ambiguous constructors)"
                    confidence = 0.50
                    deterministic = False
                elif has_var_event and distinct_ctors and not has_none_event:
                    # single known ctor type → high conf specific hint. Collapse an
                    # if/else ambiguity (e.g. patch = JsonPatch(p) / from_string(p))
                    # only when every candidate normalizes to the SAME class and
                    # there is no None/unresolved candidate (link 1 guard).
                    the_type = next(iter(distinct_ctors))
                    hint = f"{the_type}.{attr}"
                    resolved_display = hint
                    confidence = 0.80
                    deterministic = True
                else:
                    imported_hint = imported_module_attr_hint(base_expr, attr)
                    if imported_hint:
                        hint, resolved_display = imported_hint
                        confidence = 0.80
                        deterministic = True
                    elif base_expr in class_methods and attr in class_methods[base_expr]:
                        # KnownClass.method() via the class name itself (e.g. the
                        # classmethod Version.parse(...)); receiver is a class
                        # defined in this file, and attr is one of its methods.
                        hint = f"{Path(path).stem}:{base_expr}.{attr}"
                        resolved_display = hint
                        confidence = 0.80
                        deterministic = True
                    else:
                        hint = None
                        resolved_display = f"{base_expr}.{attr} (unresolved receiver)"
                        confidence = 0.40
                        deterministic = False

                caller_kind = "method" if caller in class_for_method else "fn"
                caller_id = make_id(caller_kind, caller, str(path))
                callee_id = make_id("fn", attr, str(path))
                rel = {
                    "id": f"rel:call:{caller}:{attr}:attr:{node.lineno}:{node.col_offset}",
                    "source": caller_id,
                    "target": callee_id,
                    "type": "calls",
                    "description": f"{caller} calls {attr} (ast Attribute: {dotted} -> {resolved_display})",
                    "weight": confidence,
                    "text_unit_ids": [f"tu:file:{path.name}"],
                    "human_readable_id": len(relationships) + 1,
                    "source_file": str(path),
                    "span": f"{node.lineno}:{node.col_offset}",
                    "extractor": "tree-sitter-python+ast",
                    "confidence": confidence,
                    "is_deterministic": deterministic,
                }
                if hint:
                    rel["resolved_target_hint"] = hint
                relationships.append(rel)

    # Chained-constructor method calls: `Cls(args).method(...)` -> `Cls.method`.
    # The receiver type is known statically (a class defined in this file with
    # that method), so this is a deterministic call edge the dotted-name detector
    # misses (its receiver is a Call, not a Name chain). Example: jsonpatch's
    # `AddOperation({...}).apply(obj)` -> `AddOperation.apply`.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call)):
            continue
        inner = func.value.func
        if not isinstance(inner, ast.Name):
            continue
        cls_name = inner.id
        attr = func.attr
        if cls_name not in class_methods or attr not in class_methods[cls_name]:
            continue
        caller = enclosing_function_name(node)
        if caller == "unknown":
            continue
        caller_kind = "method" if caller in class_for_method else "fn"
        hint = f"{Path(path).stem}:{cls_name}.{attr}"
        relationships.append(
            {
                "id": f"rel:call:{caller}:{attr}:ctorchain:{node.lineno}:{node.col_offset}",
                "source": make_id(caller_kind, caller, str(path)),
                "target": make_id("fn", attr, str(path)),
                "type": "calls",
                "description": f"{caller} calls {attr} (chained ctor: {cls_name}(...).{attr} -> {hint})",
                "weight": 0.8,
                "text_unit_ids": [f"tu:file:{path.name}"],
                "human_readable_id": len(relationships) + 1,
                "source_file": str(path),
                "span": f"{node.lineno}:{node.col_offset}",
                "extractor": "tree-sitter-python+ast",
                "confidence": 0.8,
                "is_deterministic": True,
                "resolved_target_hint": hint,
            }
        )


def _try_jedi_adapter(source: bytes, path: Path) -> List[Dict[str, Any]]:
    """Optional future adapter for Jedi-backed reference resolution.

    Returns an empty list when Jedi is unavailable or cannot analyze the file.
    Intended confidence tier: ~0.92, non-deterministic because it depends on
    environment/import resolution.
    """
    try:
        import jedi  # type: ignore
    except Exception:
        return []

    try:
        jedi.Script(code=source.decode("utf-8", errors="replace"), path=str(path))
    except Exception:
        return []
    return []


def _try_pyright_adapter(path: Path) -> List[Dict[str, Any]]:
    """Optional future adapter for Pyright JSON diagnostics/reference metadata.

    Returns an empty list when pyright is unavailable or fails. Intended
    confidence tier: ~0.90, non-deterministic because it depends on external
    project configuration and executable availability.
    """
    try:
        subprocess.run(
            ["pyright", "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return []
    return []


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: extract_python.py <python-file> [output.json]")
        return 1
    target = Path(argv[1])
    if not target.exists():
        print(f"Not found: {target}")
        return 2

    result = extract_from_file(target)

    out_path = Path(argv[2]) if len(argv) > 2 else Path("output/extracted.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False))

    print(f"Extracted from {target}")
    print(f"  entities: {len(result['entities'])}")
    print(f"  relationships: {len(result['relationships'])}")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
