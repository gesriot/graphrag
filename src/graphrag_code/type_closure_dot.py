"""Deterministic Graphviz DOT serialization for a bounded type-closure result.

This module renders an already-computed ``ByogGraph.type_closure`` /
``compute_uses_type_closure`` mapping. It does not open a graph, inspect a
snapshot, resolve a symbol, reread relationships, rerun BFS, reconstruct
omitted material, invoke Graphviz, spawn subprocesses, touch the network,
or write temporary files. It does not sort, expand, or truncate producer
lists. Independent ``max_nodes`` / ``max_edges`` caps may omit an
endpoint from the returned ``nodes`` list; that endpoint is given an
explicit internal identifier derived only from the returned edge record.

DOT here is a visualization **interchange** format on stdout. The project
does not render an image and does not provide an interactive UI.
Graphviz is not invoked. Node-statement order follows the returned-node
list, then missing edge endpoints in producer edge order. That is not a claim that a rendered Graphviz layout will preserve that order. Internal
identifiers ``n0000``, ``n0001``, … are serialization identifiers only:
they are not an ordinal rank field or semantic importance.

The producer is intrinsically ``uses_type``-only. The fixed edge
``label`` / ``type`` value ``uses_type`` is contract metadata, not
inferred relationship content. Stored ``source -> target`` orientation is
preserved for ``dependencies``, ``users``, and ``both``.

This is not semantic type resolution, ABI proof, runtime dispatch,
ownership, architecture, importance, hierarchy, community, GraphRAG, or
natural-language analysis.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .subgraph_dot import SubgraphDotError, quote_dot_string

TYPE_CLOSURE_DOT_SCHEMA_VERSION = 1
HARD_MAX_TYPE_CLOSURE_DOT_BYTES = 1_000_000
_GRAPH_NAME = "graphrag_type_closure"
_DIRECTIONS = ("dependencies", "users", "both")
_REQUIRED_TOP = (
    "root",
    "resolved",
    "direction",
    "max_depth",
    "max_nodes",
    "max_edges",
    "nodes",
    "edges",
    "n_nodes_total",
    "n_edges_total",
    "n_nodes_returned",
    "n_edges_returned",
    "nodes_truncated",
    "edges_truncated",
)
_REQUIRED_NODE = ("title", "depth")
_REQUIRED_EDGE = ("id", "source", "target", "depth")


class TypeClosureDotError(ValueError):
    """Invalid type-closure DOT input, unencodable value, or overflow."""


def dumps_type_closure_dot(result: Mapping[str, Any]) -> str:
    """Serialize a type-closure producer result to deterministic DOT.

    The returned string is the complete payload, including the final
    newline, and is guaranteed to be at most
    :data:`HARD_MAX_TYPE_CLOSURE_DOT_BYTES` UTF-8 bytes.
    """
    try:
        return _dumps_type_closure_dot(result)
    except SubgraphDotError as exc:
        raise TypeClosureDotError(str(exc)) from exc


def _dumps_type_closure_dot(result: Mapping[str, Any]) -> str:
    if not isinstance(result, Mapping):
        raise TypeClosureDotError("type-closure DOT input must be a mapping")
    for name in _REQUIRED_TOP:
        if name not in result:
            raise TypeClosureDotError(f"missing type-closure field {name}")

    resolved = _require_bool(result.get("resolved"), "resolved")
    direction = _require_nonempty_str(result.get("direction"), "direction")
    if direction not in _DIRECTIONS:
        raise TypeClosureDotError(f"invalid type-closure direction {direction!r}")
    max_depth = _require_int(result.get("max_depth"), "max_depth")
    max_nodes = _require_int(result.get("max_nodes"), "max_nodes")
    max_edges = _require_int(result.get("max_edges"), "max_edges")
    n_nodes_total = _require_int(result.get("n_nodes_total"), "n_nodes_total")
    n_edges_total = _require_int(result.get("n_edges_total"), "n_edges_total")
    n_nodes_returned = _require_int(
        result.get("n_nodes_returned"), "n_nodes_returned"
    )
    n_edges_returned = _require_int(
        result.get("n_edges_returned"), "n_edges_returned"
    )
    nodes_truncated = _require_bool(
        result.get("nodes_truncated"), "nodes_truncated"
    )
    edges_truncated = _require_bool(
        result.get("edges_truncated"), "edges_truncated"
    )
    nodes = _require_list(result.get("nodes"), "nodes")
    edges = _require_list(result.get("edges"), "edges")
    if n_nodes_returned != len(nodes):
        raise TypeClosureDotError("n_nodes_returned does not match nodes")
    if n_edges_returned != len(edges):
        raise TypeClosureDotError("n_edges_returned does not match edges")
    if n_nodes_returned > n_nodes_total:
        raise TypeClosureDotError("returned node count exceeds total")
    if n_edges_returned > n_edges_total:
        raise TypeClosureDotError("returned edge count exceeds total")
    if n_nodes_returned > max_nodes:
        raise TypeClosureDotError("returned node count exceeds max_nodes")
    if n_edges_returned > max_edges:
        raise TypeClosureDotError("returned edge count exceeds max_edges")
    if nodes_truncated != (n_nodes_returned < n_nodes_total):
        raise TypeClosureDotError("nodes_truncated disagrees with node counts")
    if edges_truncated != (n_edges_returned < n_edges_total):
        raise TypeClosureDotError("edges_truncated disagrees with edge counts")

    root = result.get("root")
    if resolved:
        root_title = _require_nonempty_str(root, "root")
        if n_nodes_total < 1:
            raise TypeClosureDotError("resolved type-closure must have n_nodes_total >= 1")
    else:
        if root is not None:
            raise TypeClosureDotError("unresolved type-closure must use a null root")
        if nodes or edges:
            raise TypeClosureDotError("unresolved type-closure must have empty material")
        if (
            n_nodes_total != 0
            or n_edges_total != 0
            or n_nodes_returned != 0
            or n_edges_returned != 0
        ):
            raise TypeClosureDotError("unresolved type-closure must have zero totals")
        if nodes_truncated or edges_truncated:
            raise TypeClosureDotError(
                "unresolved type-closure must not be truncated"
            )
        root_title = None

    rendered: List[Tuple[str, bool, Optional[int]]] = []
    title_to_id: Dict[str, str] = {}
    prev_node_key: Optional[Tuple[int, bytes]] = None
    for node in nodes:
        if not isinstance(node, Mapping):
            raise TypeClosureDotError("type-closure node must be a mapping")
        for name in _REQUIRED_NODE:
            if name not in node:
                raise TypeClosureDotError(f"missing node field {name}")
        title = _require_nonempty_str(node.get("title"), "node title")
        if title in title_to_id:
            raise TypeClosureDotError(f"duplicate type-closure node title {title!r}")
        depth = _require_int(node.get("depth"), "node depth")
        if depth > max_depth:
            raise TypeClosureDotError("node depth exceeds max_depth")
        key = (depth, title.encode("utf-8"))
        if prev_node_key is not None and key < prev_node_key:
            raise TypeClosureDotError("canonical producer node order")
        prev_node_key = key
        ident = f"n{len(title_to_id):04d}"
        title_to_id[title] = ident
        rendered.append((title, True, depth))

    if resolved and nodes:
        first_title, first_in_nodes, first_depth = rendered[0]
        if first_title != root_title or not first_in_nodes:
            raise TypeClosureDotError("producer node order must start with the root")
        if first_depth != 0:
            raise TypeClosureDotError("root node depth must be zero")

    parsed_edges: List[Tuple[str, str, str, int]] = []
    seen_edge_ids: set[str] = set()
    prev_edge_key: Optional[Tuple[int, bytes, bytes, bytes]] = None
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise TypeClosureDotError("type-closure edge must be a mapping")
        for name in _REQUIRED_EDGE:
            if name not in edge:
                raise TypeClosureDotError(f"missing edge field {name}")
        source = _require_nonempty_str(edge.get("source"), "edge source")
        target = _require_nonempty_str(edge.get("target"), "edge target")
        rel_id = _require_nonempty_str(edge.get("id"), "edge id")
        if rel_id in seen_edge_ids:
            raise TypeClosureDotError(f"duplicate type-closure edge id {rel_id!r}")
        seen_edge_ids.add(rel_id)
        depth = _require_int(edge.get("depth"), "edge depth")
        if depth >= max_depth:
            raise TypeClosureDotError("edge depth must be strictly below max_depth")
        key = (
            depth,
            source.encode("utf-8"),
            target.encode("utf-8"),
            rel_id.encode("utf-8"),
        )
        if prev_edge_key is not None and key < prev_edge_key:
            raise TypeClosureDotError("canonical producer edge order")
        prev_edge_key = key
        parsed_edges.append((source, target, rel_id, depth))
        for endpoint in (source, target):
            if endpoint in title_to_id:
                continue
            ident = f"n{len(title_to_id):04d}"
            title_to_id[endpoint] = ident
            rendered.append((endpoint, False, None))

    n_rendered_nodes = len(rendered)
    if n_rendered_nodes != len(title_to_id):
        raise TypeClosureDotError("rendered node identifiers are not unique")
    if n_rendered_nodes < n_nodes_returned:
        raise TypeClosureDotError("n_rendered_nodes is below n_nodes_returned")
    if n_rendered_nodes > n_nodes_total:
        raise TypeClosureDotError("n_rendered_nodes exceeds n_nodes_total")

    graph_attrs: List[Tuple[str, str]] = [
        ("schema_version", str(TYPE_CLOSURE_DOT_SCHEMA_VERSION)),
        ("resolved", _dot_bool(resolved)),
    ]
    if root_title is not None:
        graph_attrs.append(("root", root_title))
    graph_attrs.extend(
        [
            ("direction", direction),
            ("max_depth", str(max_depth)),
            ("max_nodes", str(max_nodes)),
            ("max_edges", str(max_edges)),
            ("n_nodes_total", str(n_nodes_total)),
            ("n_edges_total", str(n_edges_total)),
            ("n_nodes_returned", str(n_nodes_returned)),
            ("n_edges_returned", str(n_edges_returned)),
            ("n_rendered_nodes", str(n_rendered_nodes)),
            ("nodes_truncated", _dot_bool(nodes_truncated)),
            ("edges_truncated", _dot_bool(edges_truncated)),
        ]
    )

    node_lines: List[str] = []
    for title, in_nodes, depth in rendered:
        is_root = resolved and title == root_title
        attrs: List[Tuple[str, str]] = [
            ("label", title),
            ("title", title),
            ("in_nodes", _dot_bool(in_nodes)),
        ]
        if in_nodes:
            if depth is None:
                raise TypeClosureDotError("returned node is missing depth")
            attrs.append(("depth", str(depth)))
        attrs.append(("is_root", _dot_bool(is_root)))
        if is_root:
            attrs.append(("peripheries", "2"))
        ident = title_to_id[title]
        node_lines.append(f"  {ident} [{_format_attrs(attrs)}];")

    edge_lines: List[str] = []
    for source, target, rel_id, depth in parsed_edges:
        src_id = title_to_id[source]
        tgt_id = title_to_id[target]
        attrs = [
            ("label", "uses_type"),
            ("id", rel_id),
            ("type", "uses_type"),
            ("depth", str(depth)),
        ]
        edge_lines.append(f"  {src_id} -> {tgt_id} [{_format_attrs(attrs)}];")

    lines: List[str] = [f"digraph {_GRAPH_NAME} {{"]
    lines.append("  graph [")
    last = len(graph_attrs) - 1
    for index, (name, value) in enumerate(graph_attrs):
        suffix = "," if index != last else ""
        lines.append(f"    {_attr(name, value)}{suffix}")
    lines.append("  ];")
    lines.append(f"  {_attr('rankdir', 'LR')};")
    lines.append(f"  node [{_attr('shape', 'box')}];")
    lines.extend(node_lines)
    lines.extend(edge_lines)
    lines.append("}")
    payload = "\n".join(lines) + "\n"
    return _checked_payload(payload)


def _checked_payload(payload: str) -> str:
    if not isinstance(payload, str):
        raise TypeClosureDotError("DOT payload must be str")
    if not payload.endswith("\n") or payload.endswith("\n\n"):
        raise TypeClosureDotError("DOT payload must end with exactly one newline")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TypeClosureDotError("type-closure DOT is not strict UTF-8") from exc
    if b"\r" in encoded:
        raise TypeClosureDotError("DOT payload must not contain raw carriage returns")
    if any(byte < 0x20 and byte != 0x0A or byte == 0x7F for byte in encoded):
        raise TypeClosureDotError("DOT payload contains a raw control character")
    if len(encoded) > HARD_MAX_TYPE_CLOSURE_DOT_BYTES:
        raise TypeClosureDotError(
            f"type-closure DOT exceeds hard limit of "
            f"{HARD_MAX_TYPE_CLOSURE_DOT_BYTES} bytes"
        )
    return payload


def _format_attrs(attrs: Sequence[Tuple[str, str]]) -> str:
    return ", ".join(_attr(name, value) for name, value in attrs)


def _attr(name: str, value: str) -> str:
    if not name.isidentifier() or not name.isascii():
        raise TypeClosureDotError(f"invalid DOT attribute name {name!r}")
    return f"{name}={quote_dot_string(value)}"


def _require_list(raw: Any, name: str) -> List[Any]:
    if not isinstance(raw, list):
        raise TypeClosureDotError(f"{name} must be a list")
    return raw


def _require_bool(raw: Any, name: str) -> bool:
    if not isinstance(raw, bool):
        raise TypeClosureDotError(f"{name} must be a boolean")
    return raw


def _require_int(raw: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TypeClosureDotError(f"{name} must be an integer")
    if raw < minimum:
        raise TypeClosureDotError(f"{name} must be >= {minimum}")
    return raw


def _require_str(raw: Any, name: str) -> str:
    if not isinstance(raw, str):
        raise TypeClosureDotError(f"{name} must be a string")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise TypeClosureDotError(f"{name} is not strict UTF-8") from exc
    return raw


def _require_nonempty_str(raw: Any, name: str) -> str:
    text = _require_str(raw, name)
    if not text:
        raise TypeClosureDotError(f"{name} must be a non-empty string")
    return text


def _dot_bool(value: bool) -> str:
    return "true" if value else "false"
