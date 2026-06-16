#!/usr/bin/env python
"""
Minimal tree-sitter based Python extractor (Phase 0 prototype).

Walks a Python file and emits entity/relationship records with full provenance.

This is the foundation for turning source into the GraphRAG BYOG parquets
(entities.parquet, relationships.parquet, text_units.parquet).

Current scope (deliberately small):
- file entity
- function / class entities (top level)
- contains edges (file -> symbol)
- import edges (rough)
- conservative "calls" (name-based resolution inside the same file)

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
                for method_child in body.named_children:
                    method_node = method_child
                    if method_child.type == "decorated_definition":
                        defn = method_child.child_by_field_name("definition")
                        if defn is not None and defn.type == "function_definition":
                            method_node = defn
                        else:
                            continue
                    if method_node.type != "function_definition":
                        continue
                    method_name_node = method_node.child_by_field_name("name")
                    if method_name_node is None:
                        continue
                    method_name = get_text(source, method_name_node)
                    qualified_method = f"{name}.{method_name}"
                    method_id = make_id("method", qualified_method, source_file)
                    method_span_node = (
                        method_child
                        if method_child.type == "decorated_definition"
                        else method_node
                    )
                    method_snippet = get_text(source, method_span_node)

                    entities.append(
                        {
                            "id": method_id,
                            "title": qualified_method,
                            "type": "method",
                            "description": f"method {qualified_method} defined in {path.name}",
                            "snippet": method_snippet,
                            "text_unit_ids": [f"tu:file:{path.name}"],
                            "human_readable_id": len(entities) + 1,
                            "source_file": source_file,
                            "span": f"{method_span_node.start_point[0]+1}:{method_span_node.start_point[1]}-{method_span_node.end_point[0]+1}:{method_span_node.end_point[1]}",
                            "extractor": "tree-sitter-python",
                            "confidence": 1.0,
                            "is_deterministic": True,
                        }
                    )
                    relationships.append(
                        {
                            "id": f"rel:contains:{path.name}:{qualified_method}",
                            "source": ent_id,
                            "target": qualified_method,
                            "type": "contains",
                            "description": f"{name} contains method {method_name}",
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

    # Conservative calls: local definitions and explicitly imported names only.
    # This is syntax only - real version will need name resolution.
    def walk_calls(node: Node):
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func and func.type == "identifier":
                callee = get_text(source, func)
                if callee in callable_names:
                    # naive: assume the first defined fn that matches is caller? Better: find enclosing def
                    # For MVP we emit a relationship from "unknown-caller" or scan parents.
                    # Simpler: emit a "potential_call" edge that later passes can strengthen.
                    caller = "unknown"
                    # Walk up to find nearest function_definition
                    cur = node
                    while cur:
                        if cur.type == "function_definition":
                            nm = cur.child_by_field_name("name")
                            if nm:
                                caller = get_text(source, nm)
                            break
                        cur = cur.parent

                    if caller != "unknown" and caller in defined_names:
                        is_local = callee in defined_names
                        callee_target = (
                            make_id(defined_kinds.get(callee, "fn"), callee, source_file)
                            if is_local
                            else callee
                        )
                        relationships.append(
                            {
                                "id": f"rel:call:{caller}:{callee}:{node.start_point[0]}",
                                "source": make_id("fn", caller, source_file),
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

    # Module entity for the file (stem)
    module_title = Path(path).stem
    module_id = f"ent:module:{module_title}"
    entities.append({
        "id": module_id,
        "title": module_title,
        "type": "module",
        "description": f"Python module {module_title} (from {path.name})",
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
        "description": f"{path.name} defines module {module_title}",
        "weight": 1.0,
        "text_unit_ids": [f"tu:file:{path.name}"],
        "human_readable_id": len(relationships) + 1,
        "source_file": source_file,
        "span": "",
        "extractor": "tree-sitter-python",
        "confidence": 1.0,
        "is_deterministic": True,
    })

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

    function_spans: List[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function_spans.append(
                (node.name, node.lineno, getattr(node, "end_lineno", node.lineno))
            )

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

    def constructor_type_hint(constructor: str) -> str | None:
        if "." in constructor:
            base_expr, attr = constructor.rsplit(".", 1)
            hint, _ = module_attr_hint(base_expr, attr)
            return hint
        imported_hint = imported_callable_hint(constructor)
        if imported_hint:
            return imported_hint[0]
        return None

    class_for_method: Dict[str, str] = {}  # method_name -> ClassName for self resolution within file

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            current_class = node.name
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    class_for_method[item.name] = current_class

    # Collect assign events with lineno for reassignment guards.
    # Use the *actual enclosing function* for the assignment node (via lineno),
    # so that assignments inside nested/inner functions do not pollute the outer
    # function's scope (proper static analysis scoping).
    assign_events: Dict[str, List[tuple[int, str, str | None]]] = defaultdict(list)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets: List[ast.AST] = []
            value: ast.AST | None = None
            lineno = getattr(node, "lineno", 0)
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            if value is None:
                continue
            is_constructor = isinstance(value, ast.Call)
            for target in targets:
                if isinstance(target, ast.Name):
                    var = target.id
                    # Determine the *innermost* enclosing function for this assignment
                    # (reuses the lineno-based logic, but we compute it for every assign)
                    # We can reuse enclosing_function_name by temporarily treating it as call site
                    # (the function only uses lineno, so it works).
                    enclosing = enclosing_function_name(node)
                    if enclosing == "unknown":
                        continue
                    if is_constructor:
                        constructor = get_dotted_name(value.func)
                        if constructor:
                            assign_events[enclosing].append(
                                (lineno, var, constructor_type_hint(constructor))
                            )
                    else:
                        # reassignment to non-constructor: guard from this point onward in *this* scope
                        assign_events[enclosing].append((lineno, var, None))

    # self/cls resolution using class_for_method. Emit bridge-resolvable method
    # titles so these edges survive the two-pass FQN normalization.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if isinstance(base, ast.Name) and base.id in ("self", "cls"):
                attr = node.func.attr
                caller = enclosing_function_name(node)
                if caller in class_for_method:
                    class_name = class_for_method[caller]
                    source_title = f"{Path(path).stem}:{class_name}.{caller}"
                    hint = f"{Path(path).stem}:{class_name}.{attr}"
                    relationships.append(
                        {
                            "id": f"rel:call:{class_name}.{caller}:{base.id}.{attr}:{getattr(node, 'lineno', 0)}",
                            "source": source_title,
                            "target": hint,
                            "type": "calls",
                            "description": f"{class_name}.{caller} calls {base.id}.{attr} (self/cls method in {class_name})",
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
                if base_expr in ("self", "cls"):
                    continue
                caller = enclosing_function_name(node)
                if caller == "unknown":
                    continue

                # Reassignment guard lookup
                has_var_event = False
                var_type: str | None = None
                events = sorted(assign_events.get(caller, []), key=lambda x: x[0])
                for ev_l, ev_v, ev_t in events:
                    if ev_v == base_expr and ev_l <= getattr(node, "lineno", 0):
                        has_var_event = True
                        var_type = ev_t
                if has_var_event and var_type:
                    hint: str | None = f"{var_type}.{attr}"
                    resolved_display = hint
                    confidence = 0.80
                    deterministic = True
                elif has_var_event:
                    hint = None
                    resolved_display = f"{base_expr}.{attr} (guarded by reassignment)"
                    confidence = 0.40
                    deterministic = False
                else:
                    hint, resolved_display = module_attr_hint(base_expr, attr)
                    confidence = 0.80
                    deterministic = True

                caller_id = make_id("fn", caller, str(path))
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
