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

import json
import sys
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


def extract_from_file(path: Path) -> Dict[str, List[Dict[str, Any]]]:
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

    # Collect top-level defs
    defined_names: List[str] = []

    for child in root.children:
        if child.type in ("function_definition", "class_definition"):
            name_node = child.child_by_field_name("name")
            if name_node is None:
                continue
            name = get_text(source, name_node)
            kind = "fn" if child.type == "function_definition" else "class"
            ent_id = make_id(kind, name, source_file)

            doc = ""
            # crude docstring extraction
            body = child.child_by_field_name("body")
            if body and body.named_child_count > 0:
                first = body.named_children[0]
                if first.type == "expression_statement":
                    expr = first.named_children[0] if first.named_child_count else None
                    if expr and expr.type == "string":
                        doc = get_text(source, expr).strip('\'" \n')

            entities.append(
                {
                    "id": ent_id,
                    "title": name,
                    "type": kind,
                    "description": doc or f"{kind} {name} defined in {path.name}",
                    "text_unit_ids": [f"tu:file:{path.name}"],
                    "human_readable_id": len(entities) + 1,
                    "source_file": source_file,
                    "span": f"{child.start_point[0]+1}:{child.start_point[1]}-{child.end_point[0]+1}:{child.end_point[1]}",
                    "extractor": "tree-sitter-python",
                    "confidence": 1.0,
                    "is_deterministic": True,
                }
            )
            defined_names.append(name)

            # contains edge
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

    # Very conservative intra-file calls (look for call nodes whose function is identifier matching a defined name)
    # This is syntax only — real version will need name resolution.
    def walk_calls(node: Node):
        if node.type == "call":
            func = node.child_by_field_name("function")
            if func and func.type == "identifier":
                callee = get_text(source, func)
                if callee in defined_names:
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
                        relationships.append(
                            {
                                "id": f"rel:call:{caller}:{callee}:{node.start_point[0]}",
                                "source": make_id("fn", caller, source_file),
                                "target": make_id("fn", callee, source_file),
                                "type": "calls",
                                "description": f"{caller} may call {callee} (syntax only, name match)",
                                "weight": 0.6,
                                "text_unit_ids": [f"tu:file:{path.name}"],
                                "human_readable_id": len(relationships) + 1,
                                "source_file": source_file,
                                "span": f"{node.start_point[0]+1}:{node.start_point[1]}",
                                "extractor": "tree-sitter-python",
                                "confidence": 0.6,
                                "is_deterministic": False,  # name match only, no resolution
                            }
                        )
        for c in node.children:
            walk_calls(c)

    walk_calls(root)

    # Imports (very rough)
    for child in root.children:
        if child.type == "import_statement" or child.type == "import_from_statement":
            text = get_text(source, child)
            relationships.append(
                {
                    "id": f"rel:import:{path.name}:{len(relationships)}",
                    "source": file_id,
                    "target": f"ent:module:{text[:40]}",
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

    return {"entities": entities, "relationships": relationships}


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
