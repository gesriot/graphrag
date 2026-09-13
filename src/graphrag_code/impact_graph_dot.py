"""Deterministic Graphviz DOT serialization for a bounded impact-graph result.

This module renders an already-computed ``ByogGraph.impact_graph`` /
``compute_bounded_call_impact`` mapping. It does not open a graph, inspect a
snapshot, resolve a symbol, reread relationships, rerun BFS, reconstruct
omitted material, invoke Graphviz, spawn subprocesses, touch the network,
or write temporary files. It does not sort, expand, or truncate producer
lists. A missing returned endpoint is a renderer error, not an invented
node.

DOT here is a visualization **interchange** format on stdout. The project
does not render an image and does not provide an interactive UI.
Graphviz is not invoked. Node-statement and edge-statement order follow
the producer lists. That is not a claim that a rendered Graphviz layout
will preserve that order. Internal identifiers ``n0000``, ``n0001``, …
are serialization identifiers only: they are not an ordinal rank field
or semantic importance.

The producer is intrinsically ``calls``-only. The fixed edge
``label`` / ``type`` value ``calls`` is contract metadata, not inferred
relationship content. Stored ``source -> target`` orientation is
preserved even though traversal follows incoming calls.

This is not runtime execution proof, complete dynamic dispatch,
call-observation reconstruction, semantic impact, severity, ownership,
importance, architecture, change-risk probability, a unique path or
explanation, GraphRAG, or natural-language analysis.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .subgraph_dot import SubgraphDotError, quote_dot_string

IMPACT_GRAPH_DOT_SCHEMA_VERSION = 1
HARD_MAX_IMPACT_GRAPH_DOT_BYTES = 1_000_000
_GRAPH_NAME = "graphrag_impact_graph"
_HARD_MAX_DEPTH = 32
_HARD_MAX_NODES = 500
_HARD_MAX_EDGES = 500
_REQUIRED_TOP = (
    "root",
    "resolved",
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
_REQUIRED_NODE = (
    "title",
    "depth",
    "id",
    "type",
    "description",
    "source_file",
    "span",
    "extractor",
    "confidence",
    "is_deterministic",
)
_REQUIRED_EDGE = (
    "id",
    "source",
    "target",
    "type",
    "depth",
    "description",
    "weight",
    "source_file",
    "span",
    "extractor",
    "confidence",
    "is_deterministic",
    "fact_kind",
)


class ImpactGraphDotError(ValueError):
    """Invalid impact-graph DOT input, unencodable value, or overflow."""


def dumps_impact_graph_dot(result: Mapping[str, Any]) -> str:
    """Serialize an impact-graph producer result to deterministic DOT.

    The returned string is the complete payload, including the final
    newline, and is guaranteed to be at most
    :data:`HARD_MAX_IMPACT_GRAPH_DOT_BYTES` UTF-8 bytes.
    """
    try:
        return _dumps_impact_graph_dot(result)
    except SubgraphDotError as exc:
        raise ImpactGraphDotError(str(exc)) from exc


def _dumps_impact_graph_dot(result: Mapping[str, Any]) -> str:
    if not isinstance(result, Mapping):
        raise ImpactGraphDotError("impact-graph DOT input must be a mapping")
    _require_exact_fields(result, _REQUIRED_TOP, "impact-graph")

    resolved = _require_bool(result.get("resolved"), "resolved")
    max_depth = _require_int(
        result.get("max_depth"), "max_depth", maximum=_HARD_MAX_DEPTH
    )
    max_nodes = _require_int(
        result.get("max_nodes"),
        "max_nodes",
        minimum=1,
        maximum=_HARD_MAX_NODES,
    )
    max_edges = _require_int(
        result.get("max_edges"), "max_edges", maximum=_HARD_MAX_EDGES
    )
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
        raise ImpactGraphDotError("n_nodes_returned does not match nodes")
    if n_edges_returned != len(edges):
        raise ImpactGraphDotError("n_edges_returned does not match edges")
    if n_nodes_returned > n_nodes_total:
        raise ImpactGraphDotError("returned node count exceeds total")
    if n_edges_returned > n_edges_total:
        raise ImpactGraphDotError("returned edge count exceeds total")
    if n_nodes_returned > max_nodes or len(nodes) > max_nodes:
        raise ImpactGraphDotError("returned node count exceeds max_nodes")
    if n_edges_returned > max_edges or len(edges) > max_edges:
        raise ImpactGraphDotError("returned edge count exceeds max_edges")
    if nodes_truncated != (n_nodes_total > n_nodes_returned):
        raise ImpactGraphDotError("nodes_truncated disagrees with node counts")
    if edges_truncated != (n_edges_total > n_edges_returned):
        raise ImpactGraphDotError("edges_truncated disagrees with edge counts")

    root = result.get("root")
    if resolved:
        root_title = _require_nonempty_str(root, "root")
        if not nodes:
            raise ImpactGraphDotError("resolved impact-graph is missing the root node")
    else:
        if root is not None:
            raise ImpactGraphDotError("unresolved impact-graph must use a null root")
        if nodes or edges:
            raise ImpactGraphDotError("unresolved impact-graph must have empty material")
        if (
            n_nodes_total != 0
            or n_edges_total != 0
            or n_nodes_returned != 0
            or n_edges_returned != 0
        ):
            raise ImpactGraphDotError("unresolved impact-graph must have zero totals")
        if nodes_truncated or edges_truncated:
            raise ImpactGraphDotError(
                "unresolved impact-graph must not be truncated"
            )
        root_title = None

    graph_attrs: List[Tuple[str, str]] = [
        ("schema_version", str(IMPACT_GRAPH_DOT_SCHEMA_VERSION)),
        ("resolved", _dot_bool(resolved)),
    ]
    if root_title is not None:
        graph_attrs.append(("root", root_title))
    graph_attrs.extend(
        [
            ("relationship_type", "calls"),
            ("max_depth", str(max_depth)),
            ("max_nodes", str(max_nodes)),
            ("max_edges", str(max_edges)),
            ("n_nodes_total", str(n_nodes_total)),
            ("n_edges_total", str(n_edges_total)),
            ("n_nodes_returned", str(n_nodes_returned)),
            ("n_edges_returned", str(n_edges_returned)),
            ("nodes_truncated", _dot_bool(nodes_truncated)),
            ("edges_truncated", _dot_bool(edges_truncated)),
        ]
    )

    title_to_id: Dict[str, str] = {}
    depth_of: Dict[str, int] = {}
    node_lines: List[str] = []
    prev_rest: Optional[Tuple[int, bytes]] = None
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            raise ImpactGraphDotError("impact-graph node must be a mapping")
        _require_exact_fields(node, _REQUIRED_NODE, "node")
        title = _require_nonempty_str(node.get("title"), "node title")
        if title in title_to_id:
            raise ImpactGraphDotError(f"duplicate impact-graph node title {title!r}")
        depth = _require_int(
            node.get("depth"), "node depth", maximum=max_depth
        )
        if index == 0:
            if resolved and (title != root_title or depth != 0):
                raise ImpactGraphDotError(
                    "producer node order must start with the root at depth 0"
                )
        else:
            key = (depth, title.encode("utf-8"))
            if prev_rest is not None and key < prev_rest:
                raise ImpactGraphDotError("canonical producer node order")
            prev_rest = key
        ident = f"n{index:04d}"
        title_to_id[title] = ident
        depth_of[title] = depth
        is_root = resolved and title == root_title
        attrs: List[Tuple[str, str]] = [
            ("label", title),
            ("title", title),
            ("depth", str(depth)),
        ]
        node_type = node.get("type")
        if node_type is not None:
            attrs.append(("type", _require_str(node_type, "node type")))
        attrs.append(("is_root", _dot_bool(is_root)))
        if is_root:
            attrs.append(("peripheries", "2"))
        node_lines.append(f"  {ident} [{_format_attrs(attrs)}];")

    if resolved:
        if title_to_id.get(root_title) != "n0000":
            raise ImpactGraphDotError("root must use identifier n0000")
        if list(title_to_id).count(root_title) != 1:
            raise ImpactGraphDotError("root must appear exactly once")

    edge_lines: List[str] = []
    seen_edge_ids: set[str] = set()
    prev_edge: Optional[Tuple[int, bytes, bytes, bytes, bytes]] = None
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise ImpactGraphDotError("impact-graph edge must be a mapping")
        _require_exact_fields(edge, _REQUIRED_EDGE, "edge")
        source = _require_nonempty_str(edge.get("source"), "edge source")
        target = _require_nonempty_str(edge.get("target"), "edge target")
        rel_type = _require_nonempty_str(edge.get("type"), "edge type")
        if rel_type != "calls":
            raise ImpactGraphDotError("impact-graph edge type must be exactly 'calls'")
        rel_id = _require_nonempty_str(edge.get("id"), "edge id")
        if rel_id in seen_edge_ids:
            raise ImpactGraphDotError(f"duplicate impact-graph edge id {rel_id!r}")
        seen_edge_ids.add(rel_id)
        depth = _require_int(
            edge.get("depth"), "edge depth", maximum=max_depth
        )
        src_id = title_to_id.get(source)
        tgt_id = title_to_id.get(target)
        if src_id is None or tgt_id is None:
            raise ImpactGraphDotError(
                "impact-graph edge endpoint is not a returned node"
            )
        expected_depth = min(depth_of[source], depth_of[target])
        if depth != expected_depth:
            raise ImpactGraphDotError("edge depth must equal min(endpoint depths)")
        key = (
            depth,
            source.encode("utf-8"),
            target.encode("utf-8"),
            rel_type.encode("utf-8"),
            rel_id.encode("utf-8"),
        )
        if prev_edge is not None and key < prev_edge:
            raise ImpactGraphDotError("canonical producer edge order")
        prev_edge = key
        attrs = [
            ("label", "calls"),
            ("id", rel_id),
            ("type", "calls"),
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
        raise ImpactGraphDotError("DOT payload must be str")
    if not payload.endswith("\n") or payload.endswith("\n\n"):
        raise ImpactGraphDotError("DOT payload must end with exactly one newline")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ImpactGraphDotError("impact-graph DOT is not strict UTF-8") from exc
    if b"\r" in encoded:
        raise ImpactGraphDotError("DOT payload must not contain raw carriage returns")
    if any(byte < 0x20 and byte != 0x0A or byte == 0x7F for byte in encoded):
        raise ImpactGraphDotError("DOT payload contains a raw control character")
    if len(encoded) > HARD_MAX_IMPACT_GRAPH_DOT_BYTES:
        raise ImpactGraphDotError(
            f"impact-graph DOT exceeds hard limit of "
            f"{HARD_MAX_IMPACT_GRAPH_DOT_BYTES} bytes"
        )
    return payload


def _format_attrs(attrs: Sequence[Tuple[str, str]]) -> str:
    return ", ".join(_attr(name, value) for name, value in attrs)


def _attr(name: str, value: str) -> str:
    if not name.isidentifier() or not name.isascii():
        raise ImpactGraphDotError(f"invalid DOT attribute name {name!r}")
    return f"{name}={quote_dot_string(value)}"


def _require_exact_fields(
    raw: Mapping[str, Any], fields: Tuple[str, ...], kind: str
) -> None:
    got = set(raw)
    expected = set(fields)
    missing = sorted(expected - got)
    extra = sorted(got - expected)
    if missing:
        raise ImpactGraphDotError(f"missing {kind} field {missing[0]}")
    if extra:
        raise ImpactGraphDotError(f"unexpected {kind} field {extra[0]}")


def _require_list(raw: Any, name: str) -> List[Any]:
    if not isinstance(raw, list):
        raise ImpactGraphDotError(f"{name} must be a list")
    return raw


def _require_bool(raw: Any, name: str) -> bool:
    if not isinstance(raw, bool):
        raise ImpactGraphDotError(f"{name} must be a boolean")
    return raw


def _require_int(
    raw: Any, name: str, *, minimum: int = 0, maximum: Optional[int] = None
) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ImpactGraphDotError(f"{name} must be an integer")
    if raw < minimum:
        raise ImpactGraphDotError(f"{name} must be >= {minimum}")
    if maximum is not None and raw > maximum:
        raise ImpactGraphDotError(f"{name} must be <= {maximum}")
    return raw


def _require_str(raw: Any, name: str) -> str:
    if not isinstance(raw, str):
        raise ImpactGraphDotError(f"{name} must be a string")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ImpactGraphDotError(f"{name} is not strict UTF-8") from exc
    return raw


def _require_nonempty_str(raw: Any, name: str) -> str:
    text = _require_str(raw, name)
    if not text:
        raise ImpactGraphDotError(f"{name} must be a non-empty string")
    return text


def _dot_bool(value: bool) -> str:
    return "true" if value else "false"
