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
    import_orig: Dict[str, str] = {}  # local/alias name -> original imported symbol
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
                # Aliased imports (e.g. `_gettext as _`): resolve calls to the
                # alias back to the original symbol so the hint names a real
                # entity (i18n:_gettext, not i18n:_).
                import_orig[local] = alias.name
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
            orig = import_orig.get(name, name)
            return f"{module_title(module)}:{orig}", f"{module}.{orig}"
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
            # Retain a class with no direct methods: it can still inherit a
            # same-file member, which is a real dispatch target.
            class_methods[node.name]
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
    # Qualified names of @property / @x.setter / @x.deleter bodies. Import-ctor
    # edges from these collide when getter and setter share a title
    # (MoveOperation.from_key), which trips span_outside_caller in the audit.
    property_accessor_qnames: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decos = item.decorator_list
            if any(isinstance(d, ast.Name) and d.id in ("property", "cached_property") for d in decos):
                property_methods.add((node.name, item.name))
            is_prop_accessor = any(
                (isinstance(d, ast.Name) and d.id in ("property", "cached_property"))
                or (isinstance(d, ast.Attribute) and d.attr in ("setter", "getter", "deleter"))
                for d in decos
            )
            if is_prop_accessor:
                property_accessor_qnames.add(f"{node.name}.{item.name}")
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

    # `references` edges: a module-level data table whose RHS names other entities
    # (other data, defined functions/classes, or imported symbols incl. aliases)
    # depends on them, e.g. humanize's `human_powers = (NS_("thousand", ...), ...)`
    # references `i18n:_ngettext_noop`, and `_SUPERSCRIPT_TRANS = maketrans(
    # _SUPERSCRIPT_MAP)` references `_SUPERSCRIPT_MAP`. The closure follows these so
    # a data table's own dependencies are packed, not just the table.
    def _ref_title(nm: str) -> str | None:
        if nm in data_names or nm in set(defined_names):
            return f"{Path(path).stem}:{nm}"
        if nm in import_map:
            return f"{module_title(import_map[nm])}:{import_orig.get(nm, nm)}"
        return None

    seen_ref_edges: set[tuple[str, str]] = set()
    for stmt in getattr(tree, "body", []):
        targets = (
            stmt.targets if isinstance(stmt, ast.Assign)
            else [stmt.target] if isinstance(stmt, ast.AnnAssign) else []
        )
        dnames = [t.id for t in targets if isinstance(t, ast.Name) and t.id in data_names]
        if not dnames or getattr(stmt, "value", None) is None:
            continue
        ref_titles: set[str] = set()
        for sub in ast.walk(stmt.value):
            nm = sub.id if isinstance(sub, ast.Name) else None
            if nm:
                rt = _ref_title(nm)
                if rt:
                    ref_titles.add(rt)
        for dname in dnames:
            src = make_id("data", dname, str(path))
            for rt in sorted(ref_titles):
                if rt == f"{Path(path).stem}:{dname}" or (src, rt) in seen_ref_edges:
                    continue
                seen_ref_edges.add((src, rt))
                relationships.append(
                    {
                        "id": f"rel:references:{dname}:{rt}",
                        "source": src,
                        "target": rt,
                        "type": "references",
                        "description": f"data {dname} references {rt}",
                        "weight": 0.90,
                        "text_unit_ids": [f"tu:file:{path.name}"],
                        "human_readable_id": len(relationships) + 1,
                        "source_file": str(path),
                        "span": f"{getattr(stmt, 'lineno', 0)}",
                        "extractor": "python-ast",
                        "confidence": 0.90,
                        "is_deterministic": True,
                        "resolved_target_hint": rt,
                    }
                )

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

    # -----------------------------------------------------------------
    # Cross-module imported types via parameter defaults + self attrs.
    # Pattern: `from jsonpointer import JsonPointer` plus
    #   def __init__(self, ..., pointer_cls=JsonPointer):
    #       self.pointer_cls = pointer_cls
    #       self.pointer = self.pointer_cls(path)
    #   def apply(self, obj):
    #       self.pointer.to_last(obj)
    # Each step is a static fact once the default is an imported (or same-file)
    # class name. Ambiguous multi-ctor cases still leave no hint.
    # -----------------------------------------------------------------
    # qualified func -> param -> "module:Class" type title
    func_param_types: Dict[str, Dict[str, str]] = defaultdict(dict)

    def _function_qname(fn: ast.AST) -> str:
        lineno = getattr(fn, "lineno", -1)
        end = getattr(fn, "end_lineno", lineno)
        # Prefer exact span match for this function (not a nested child).
        for name, start, stop in function_spans:
            if start == lineno and stop == end:
                return name
        return enclosing_function_name(fn)

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        qname = _function_qname(node)
        args = list(node.args.args)  # includes self/cls
        defaults = list(node.args.defaults)
        # defaults align to the last N positional args
        if not defaults:
            continue
        paired = list(zip(args[-len(defaults) :], defaults))
        for arg, default in paired:
            if not isinstance(default, ast.Name):
                continue
            hint = constructor_type_hint(default.id)
            if hint:
                func_param_types[qname][arg.arg] = hint

    # class simple name -> attr -> type title (imported or local class entity)
    class_attr_types: Dict[str, Dict[str, str]] = defaultdict(dict)
    # class simple name -> base simple names (same-file only)
    class_bases: Dict[str, List[str]] = defaultdict(list)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for b in node.bases:
            if isinstance(b, ast.Name):
                class_bases[node.name].append(b.id)

    def _lookup_class_attr_type(class_name: str, attr: str, *, _seen: set | None = None) -> str | None:
        """Resolve self.<attr>'s static type, walking same-file bases."""
        _seen = _seen or set()
        simple = class_name.split(".")[-1]
        if simple in _seen:
            return None
        _seen.add(simple)
        if attr in class_attr_types.get(simple, {}):
            return class_attr_types[simple][attr]
        for base in class_bases.get(simple, []):
            found = _lookup_class_attr_type(base, attr, _seen=_seen)
            if found:
                return found
        return None

    def _type_from_call_func(func: ast.AST, enclosing: str) -> str | None:
        """Type produced by calling func(...) when statically known."""
        dotted = get_dotted_name(func)
        if not dotted:
            return None
        # bare ImportedClass(...) / LocalClass(...)
        if "." not in dotted:
            return constructor_type_hint(dotted)
        # self.pointer_cls(...) / cls.pointer_cls(...)
        root, _, rest = dotted.partition(".")
        if root in ("self", "cls") and rest and "." not in rest:
            if enclosing in class_for_method:
                return _lookup_class_attr_type(class_for_method[enclosing], rest)
        return constructor_type_hint(dotted)

    # First pass: self.attr = <known type source>
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = list(node.targets) if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if value is None:
            continue
        enclosing = enclosing_function_name(node)
        if enclosing == "unknown" or enclosing not in class_for_method:
            continue
        class_name = class_for_method[enclosing].split(".")[-1]
        for target in targets:
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id in ("self", "cls")
            ):
                continue
            attr = target.attr
            effective: str | None = None
            if isinstance(value, ast.Name):
                # self.pointer_cls = pointer_cls  (param with imported default)
                if value.id in func_param_types.get(enclosing, {}):
                    effective = func_param_types[enclosing][value.id]
                else:
                    effective = constructor_type_hint(value.id)
            elif isinstance(value, ast.Call):
                effective = _type_from_call_func(value.func, enclosing)
            if effective:
                class_attr_types[class_name][attr] = effective

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
            enclosing = enclosing_function_name(node)
            for target in targets:
                # Track self.attr = Ctor(...) as class_attr type (second chance after param pass).
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in ("self", "cls")
                    and enclosing in class_for_method
                    and is_constructor
                    and value is not None
                ):
                    class_name = class_for_method[enclosing].split(".")[-1]
                    t = _type_from_call_func(value.func, enclosing)
                    if t:
                        class_attr_types[class_name][target.attr] = t
                if isinstance(target, ast.Name):
                    var = target.id
                    if enclosing == "unknown":
                        continue
                    effective: str | None = None
                    if container_kind:
                        effective = f"container:{container_kind}"
                    elif is_constructor and value is not None:
                        effective = _type_from_call_func(value.func, enclosing)
                        if effective is None:
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
                    method_bare = caller.split(".")[-1] if "." in caller else caller
                    source_title = f"{Path(path).stem}:{class_name}.{method_bare}"
                    is_direct = base_dotted in ("self", "cls")
                    is_known_same_class_method = attr in class_methods.get(simple_class, set())

                    # self.pointer_cls(...) where pointer_cls holds an imported type
                    # → constructor body (Class.__init__), not a same-class method.
                    # Target is ``.__init__`` so the calls closure can enter the
                    # class body (e.g. JsonPointer.__init__ → unescape). The
                    # call-graph oracle normalizes ``.__init__`` ↔ class.
                    if is_direct:
                        stored_type = _lookup_class_attr_type(simple_class, attr)
                        if stored_type:
                            # Skip property getter/setter bodies: they share a
                            # title with their pair and create audit span clashes.
                            if caller not in property_accessor_qnames:
                                # Class entity (type identity) + __init__ (body entry).
                                # Both needed: oracle normalizes them together;
                                # adequacy lists each as a distinct must-reach.
                                for tag_suffix, ctor_target in (
                                    ("class", stored_type),
                                    ("init", f"{stored_type}.__init__"),
                                ):
                                    relationships.append(
                                        {
                                            "id": (
                                                f"rel:call:{class_name}.{method_bare}:"
                                                f"import-ctor-{tag_suffix}:{attr}:"
                                                f"{getattr(node, 'lineno', 0)}"
                                            ),
                                            "source": source_title,
                                            "target": ctor_target,
                                            "type": "calls",
                                            "description": (
                                                f"{class_name}.{method_bare} constructs "
                                                f"{ctor_target} via self.{attr} "
                                                f"(imported type default)"
                                            ),
                                            "weight": 0.80,
                                            "text_unit_ids": [f"tu:file:{path.name}"],
                                            "human_readable_id": len(relationships) + 1,
                                            "source_file": str(path),
                                            "span": f"{getattr(node, 'lineno', 0)}",
                                            "extractor": "tree-sitter-python+ast",
                                            "confidence": 0.80,
                                            "is_deterministic": True,
                                            "resolved_target_hint": ctor_target,
                                        }
                                    )
                            continue

                    # self.pointer.to_last(...) where self.pointer's type is known
                    # (e.g. jsonpointer:JsonPointer from the assignment above).
                    if (
                        not is_direct
                        and base_dotted.count(".") == 1
                        and root_base in ("self", "cls")
                    ):
                        obj_attr = base_dotted.split(".", 1)[1]
                        obj_type = _lookup_class_attr_type(simple_class, obj_attr)
                        if obj_type:
                            hint = f"{obj_type}.{attr}"
                            relationships.append(
                                {
                                    "id": f"rel:call:{class_name}.{method_bare}:typed-self:{obj_attr}.{attr}:{getattr(node, 'lineno', 0)}",
                                    "source": source_title,
                                    "target": hint,
                                    "type": "calls",
                                    "description": (
                                        f"{class_name}.{method_bare} calls {base_dotted}.{attr} "
                                        f"(self.{obj_attr}: {obj_type})"
                                    ),
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
                            continue

                    # Direct self.method()/cls.method() is strong. Chained
                    # self.foo.method()/cls.foo.method() is still useful for
                    # singleton/cache patterns (sqlparse's
                    # cls._default_instance.default_initialization()), but only
                    # promote it if the called attr is actually a method on the
                    # same class.
                    if is_direct or is_known_same_class_method:
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

    # -----------------------------------------------------------------
    # Inheritance edges (NOT calls). Adequacy closure already lists
    # ``inherits`` in CLOSURE_EDGES; without these the type is empty forever.
    #
    # Rule: for ``class Child(Parent):`` where Parent is a class defined in
    # *this file* (not a builtin like ``object``), emit
    #   Child --inherits--> Parent
    # with is_deterministic=True. Cross-module bases are left unresolved
    # (import aliasing / re-exports would be a guess). This is a type-graph
    # fact, not a call — do not overload ``calls``.
    # -----------------------------------------------------------------
    _BUILTIN_BASES = {
        "object", "type", "Exception", "BaseException", "dict", "list", "set",
        "tuple", "str", "int", "float", "bool", "bytes", "set", "frozenset",
        "enum", "Enum", "IntEnum", "ABC", "ABCMeta",
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        child = node.name
        if child not in local_classes:
            continue
        for b in node.bases:
            if not isinstance(b, ast.Name):
                continue  # skip pkg.Base / generics — not a bare same-file name
            parent = b.id
            if parent in _BUILTIN_BASES or parent not in local_classes:
                continue
            relationships.append(
                {
                    "id": f"rel:inherits:{child}:{parent}:{getattr(node, 'lineno', 0)}",
                    "source": f"{Path(path).stem}:{child}",
                    "target": f"{Path(path).stem}:{parent}",
                    "type": "inherits",
                    "description": (
                        f"{child} inherits {parent} "
                        f"(AST ClassDef base; not a calls edge)"
                    ),
                    "weight": 0.95,
                    "text_unit_ids": [f"tu:file:{path.name}"],
                    "human_readable_id": len(relationships) + 1,
                    "source_file": str(path),
                    "span": f"{getattr(node, 'lineno', 0)}",
                    "extractor": "tree-sitter-python+ast",
                    "confidence": 0.95,
                    "is_deterministic": True,
                    "resolved_target_hint": f"{Path(path).stem}:{parent}",
                }
            )

    def _same_file_mro(simple_class: str, active: set[str] | None = None) -> list[str] | None:
        """Return a C3 linearization when every non-builtin base is local.

        An inherited-member edge asserts which declaration Python will execute,
        so partial MRO knowledge is not enough.  Builtin bases terminate the
        local portion; an imported/unknown base or inconsistent local MRO emits
        no member edges rather than guessing an override winner.
        """
        active = active or set()
        if simple_class in active:
            return None
        active = active | {simple_class}
        direct = [
            base for base in class_bases.get(simple_class, []) if base not in _BUILTIN_BASES
        ]
        if any(base not in local_classes for base in direct):
            return None
        parent_mros: list[list[str]] = []
        for base in direct:
            parent_mro = _same_file_mro(base, active)
            if parent_mro is None:
                return None
            parent_mros.append(parent_mro)
        sequences = [mro[:] for mro in parent_mros] + [direct[:]]
        linearized = [simple_class]
        while any(sequences):
            candidate = next(
                (
                    sequence[0]
                    for sequence in sequences
                    if sequence and not any(sequence[0] in other[1:] for other in sequences)
                ),
                None,
            )
            if candidate is None:
                return None
            linearized.append(candidate)
            for sequence in sequences:
                if sequence and sequence[0] == candidate:
                    sequence.pop(0)
        return linearized

    # Inherited-member rule: a reached subclass may expose one *effective*
    # same-file base member for every direct member it does not override.  The
    # edge is ``inherits``, not ``calls``: it records dispatch identity without
    # claiming a source-level call happened.  This is deliberately narrower
    # than class expansion — a reached class never pulls in its own members,
    # only the declarations it inherits.  Unknown/cross-module MROs are left
    # unresolved, and a subclass declaration always suppresses the base edge.
    class_lines = {
        node.name: getattr(node, "lineno", 0)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }
    source_module = Path(path).stem
    for child in sorted(local_classes):
        linearized = _same_file_mro(child)
        if linearized is None or len(linearized) < 2:
            continue
        child_members = class_methods.get(child, set())
        inherited_members: dict[str, str] = {}
        for defining_class in linearized[1:]:
            for member in sorted(class_methods.get(defining_class, set())):
                if member not in child_members and member not in inherited_members:
                    inherited_members[member] = defining_class
        for member, defining_class in inherited_members.items():
            target = f"{source_module}:{defining_class}.{member}"
            relationships.append(
                {
                    "id": (
                        f"rel:inherits-member:{child}:{defining_class}.{member}:"
                        f"{class_lines.get(child, 0)}"
                    ),
                    "source": f"{source_module}:{child}",
                    "target": target,
                    "type": "inherits",
                    "description": (
                        f"{child} inherits unoverridden member "
                        f"{defining_class}.{member} (same-file C3 MRO; not a call)"
                    ),
                    "weight": 0.95,
                    "text_unit_ids": [f"tu:file:{path.name}"],
                    "human_readable_id": len(relationships) + 1,
                    "source_file": str(path),
                    "span": f"{class_lines.get(child, 0)}",
                    "extractor": "tree-sitter-python+ast",
                    "confidence": 0.95,
                    "is_deterministic": True,
                    "resolved_target_hint": target,
                }
            )

    def _nearest_defining_class(simple_class: str, member: str, *, kind: str) -> str | None:
        """Walk same-file MRO for a class that defines ``member``.

        kind='method' looks in class_methods; kind='property' looks in property_methods.
        """
        seen: set[str] = set()
        stack = [simple_class]
        while stack:
            cur = stack.pop(0)
            if cur in seen:
                continue
            seen.add(cur)
            if kind == "method" and member in class_methods.get(cur, set()):
                return cur
            if kind == "property" and (cur, member) in property_methods:
                return cur
            stack.extend(class_bases.get(cur, []))
        return None

    # Property bridge: a method reading self.<name>/cls.<name> where <name> is an
    # @property on the same class *or a same-file base* is a property edge
    # (NOT ``calls``). Inherited properties resolve to the defining base
    # (RemoveOperation reading self.path → PatchOperation.path).
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
        defining = _nearest_defining_class(simple_class, node.attr, kind="property")
        if defining is None:
            continue
        method_bare = caller.split(".")[-1] if "." in caller else caller
        source_title = f"{Path(path).stem}:{class_name}.{method_bare}"
        hint = f"{Path(path).stem}:{defining}.{node.attr}"
        dedupe = (source_title, hint)
        if dedupe in seen_property_edges:
            continue
        seen_property_edges.add(dedupe)
        relationships.append(
            {
                "id": f"rel:property:{class_name}.{method_bare}:{defining}.{node.attr}:{getattr(node, 'lineno', 0)}",
                "source": source_title,
                "target": hint,
                "type": "property",
                "description": (
                    f"{class_name}.{method_bare} reads @property {defining}.{node.attr}"
                    + (" (inherited)" if defining != simple_class else "")
                ),
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
            orig = import_orig.get(bare, bare)
            rel["resolved_target_hint"] = f"{module_stem}:{orig}"
            rel["description"] = f"{rel.get('description', '')} (ast import hint: {module}.{orig})"
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
    # Same-file classes are verified against class_methods; imported classes
    # (``from jsonpointer import JsonPointer``; ``JsonPointer(p).to_last(o)``)
    # are resolved optimistically — the bridge drops the edge if the target
    # entity is absent.
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
        if cls_name in class_methods:
            if attr not in class_methods[cls_name]:
                continue
            hint = f"{Path(path).stem}:{cls_name}.{attr}"
        elif cls_name in import_map:
            orig = import_orig.get(cls_name, cls_name)
            hint = f"{module_title(import_map[cls_name])}:{orig}.{attr}"
        else:
            continue
        caller = enclosing_function_name(node)
        if caller == "unknown":
            continue
        caller_kind = "method" if caller in class_for_method else "fn"
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

    # Constructor -> __init__ and operator -> dunder edges for same-file classes.
    # `Cls(...)` invokes `Cls.__init__`, and `Cls(...) - x` / `-Cls(...)` invokes
    # `Cls.__sub__` / `Cls.__neg__`. The closure otherwise reaches only the class
    # entity (pack-excluded as a broad span), so a porter never sees the fields or
    # operator semantics it needs. These are precise member edges, not a class pack.
    _binop_dunder = {
        ast.Add: "__add__", ast.Sub: "__sub__", ast.Mult: "__mul__",
        ast.Div: "__truediv__", ast.FloorDiv: "__floordiv__", ast.Mod: "__mod__",
        ast.Pow: "__pow__",
    }
    _unaryop_dunder = {ast.USub: "__neg__", ast.UAdd: "__pos__"}

    def _class_member_hint(cls_name: str, member: str) -> str | None:
        """Resolve `Cls.member` to an entity title for a same-file OR imported class.

        Same-file members are verified against class_methods. Imported classes
        are optimistic (bridge drops if the target entity is absent).

        Constructors: target ``Cls.__init__`` when Cls defines it; if Cls has no
        own ``__init__``, walk same-file bases and target the defining base's
        ``__init__`` (what the frame actually runs). Imported classes use
        ``Mod:Cls.__init__``; the call-graph oracle normalizes ``.__init__`` ↔
        class for scoring, so observation matching still holds.
        """
        if cls_name in local_classes:
            if member == "__init__":
                defining = _nearest_defining_class(cls_name, "__init__", kind="method")
                if defining is None:
                    return None
                return f"{Path(path).stem}:{defining}.__init__"
            if member in class_methods.get(cls_name, set()):
                return f"{Path(path).stem}:{cls_name}.{member}"
            return None
        if cls_name in import_map:
            orig = import_orig.get(cls_name, cls_name)
            mod = module_title(import_map[cls_name])
            if member == "__init__":
                return f"{mod}:{orig}.__init__"
            return f"{mod}:{orig}.{member}"
        return None

    def _emit_member_edge(node: ast.AST, cls_name: str, member: str, tag: str) -> None:
        hint = _class_member_hint(cls_name, member)
        if hint is None:
            return
        caller = enclosing_function_name(node)
        if caller == "unknown":
            return
        caller_kind = "method" if caller in class_for_method else "fn"
        targets = [hint]
        # Construction: also name the class entity when the body target is __init__
        # so the type identity stays on the closure (spec lists both).
        if member == "__init__" and hint.endswith(".__init__"):
            class_title = hint[: -len(".__init__")]
            if class_title not in targets:
                targets.append(class_title)
        for ti, tgt in enumerate(targets):
            relationships.append(
                {
                    "id": (
                        f"rel:call:{caller}:{cls_name}.{member}:{tag}{ti}:"
                        f"{getattr(node,'lineno',0)}:{getattr(node,'col_offset',0)}"
                    ),
                    "source": make_id(caller_kind, caller, str(path)),
                    "target": tgt,
                    "type": "calls",
                    "description": f"{caller} uses {tgt} ({tag}; via {cls_name})",
                    "weight": 0.8,
                    "text_unit_ids": [f"tu:file:{path.name}"],
                    "human_readable_id": len(relationships) + 1,
                    "source_file": str(path),
                    "span": f"{getattr(node,'lineno',0)}:{getattr(node,'col_offset',0)}",
                    "extractor": "tree-sitter-python+ast",
                    "confidence": 0.8,
                    "is_deterministic": True,
                    "resolved_target_hint": tgt,
                }
            )

    def _ctor_class(expr: ast.AST) -> str | None:
        if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
            name = expr.func.id
            return name if (name in local_classes or name in import_map) else None
        return None

    def _type_title_of_expr(expr: ast.AST, enclosing: str) -> str | None:
        """Static type title of an expression when known without guessing."""
        # Name with a single tracked ctor type in this function.
        if isinstance(expr, ast.Name):
            events = sorted(assign_events.get(enclosing, []), key=lambda x: x[0])
            last = None
            distinct: set[str] = set()
            saw_none = False
            for _l, v, t in events:
                if v != expr.id:
                    continue
                last = t
                if t is None:
                    saw_none = True
                elif not str(t).startswith("container:") and not str(t).startswith("ambiguous:"):
                    distinct.add(t)
            if last and not saw_none and len(distinct) == 1:
                return next(iter(distinct))
            if last and not str(last).startswith("container:") and not saw_none and len(distinct) <= 1:
                return last
            return constructor_type_hint(expr.id)
        # self.attr with class_attr_types
        dotted = get_dotted_name(expr)
        if dotted and dotted.count(".") == 1:
            root, attr = dotted.split(".", 1)
            if root in ("self", "cls") and enclosing in class_for_method:
                return _lookup_class_attr_type(class_for_method[enclosing], attr)
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in local_classes or node.func.id in import_map:
                _emit_member_edge(node, node.func.id, "__init__", "ctor")
        elif isinstance(node, ast.BinOp):
            dunder = _binop_dunder.get(type(node.op))
            if dunder:
                cls = _ctor_class(node.left)
                if cls:
                    _emit_member_edge(node, cls, dunder, "binop")
        elif isinstance(node, ast.UnaryOp):
            dunder = _unaryop_dunder.get(type(node.op))
            if dunder:
                cls = _ctor_class(node.operand)
                if cls:
                    _emit_member_edge(node, cls, dunder, "unaryop")
        elif isinstance(node, ast.Compare):
            # self.pointer == from_ptr → JsonPointer.__eq__ when left is typed.
            enclosing = enclosing_function_name(node)
            if enclosing == "unknown":
                continue
            left_type = _type_title_of_expr(node.left, enclosing)
            if not left_type:
                continue
            for op in node.ops:
                if isinstance(op, (ast.Eq, ast.NotEq)):
                    member = "__eq__" if isinstance(op, ast.Eq) else "__ne__"
                    hint = f"{left_type}.{member}"
                    caller_kind = "method" if enclosing in class_for_method else "fn"
                    relationships.append(
                        {
                            "id": (
                                f"rel:call:{enclosing}:{left_type}.{member}:compare:"
                                f"{getattr(node,'lineno',0)}:{getattr(node,'col_offset',0)}"
                            ),
                            "source": make_id(caller_kind, enclosing, str(path)),
                            "target": hint,
                            "type": "calls",
                            "description": (
                                f"{enclosing} uses {left_type}.{member} "
                                f"(compare on typed receiver)"
                            ),
                            "weight": 0.8,
                            "text_unit_ids": [f"tu:file:{path.name}"],
                            "human_readable_id": len(relationships) + 1,
                            "source_file": str(path),
                            "span": f"{getattr(node,'lineno',0)}:{getattr(node,'col_offset',0)}",
                            "extractor": "tree-sitter-python+ast",
                            "confidence": 0.8,
                            "is_deterministic": True,
                            "resolved_target_hint": hint,
                        }
                    )
                    break


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
