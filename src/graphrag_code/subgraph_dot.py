"""Deterministic Graphviz DOT serialization for a bounded subgraph result.

This module renders an already-computed ``ByogGraph.subgraph`` /
``compute_bounded_subgraph`` mapping. It does not open a graph, traverse
relationships, invoke Graphviz, spawn subprocesses, touch the network, or
write temporary files.

DOT here is a visualization **interchange** format on stdout. The project
does not render an image and does not provide an interactive UI.

Contract
========

* Directed, non-strict ``digraph graphrag_subgraph``. Parallel stored
  relationships stay separate statements; self-edges render once.
* Internal node identifiers are ``n0000``, ``n0001``, … in **producer
  node order**. Raw titles are never identifiers.
* Node and edge statements preserve producer order. This renderer does
  not sort or truncate.
* Every returned edge names the internal identifiers of its returned
  source and target. A missing endpoint is a renderer error, not an
  omitted statement.
* Direction is metadata only. Edges are always ``source -> target`` in
  stored orientation.
* The graph-level ``edge_types`` value is canonical JSON text: ``null``
  means no filter and a non-empty JSON array preserves exact filter-item
  boundaries (including commas and a literal ``"all"`` relationship type).
* Dynamic strings are emitted only as quoted DOT strings through
  :func:`quote_dot_string`. No stored value is interpolated into an
  identifier, raw attribute, or comment.
* Control characters never appear raw. ``\\``, ``\"``, ``\\n``, ``\\r``,
  and ``\\t`` use those Graphviz escapes; remaining C0/C1/DEL controls
  use ``\\xHH``. Values that cannot be encoded as strict UTF-8 fail closed.
* Optional stored descriptions, snippets, spans, weights, confidence, and
  other dataframe columns are omitted.
* Hard limit: :data:`HARD_MAX_SUBGRAPH_DOT_BYTES` UTF-8 bytes for the
  complete payload, including the final newline. Overflow fails before
  any caller write. The successful payload ends with exactly one newline.

Unresolved or ambiguous producer results (``resolved=false``, empty
material, zero totals) render as a valid empty digraph. That is not an
error.
"""

from __future__ import annotations

import json
from typing import Any, List, Mapping, Sequence, Tuple

SUBGRAPH_DOT_SCHEMA_VERSION = 1
HARD_MAX_SUBGRAPH_DOT_BYTES = 1_000_000
_GRAPH_NAME = "graphrag_subgraph"
_CONTROL_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


class SubgraphDotError(ValueError):
    """Invalid subgraph DOT input, unencodable value, or byte-limit overflow."""


def quote_dot_string(value: str) -> str:
    """Return ``value`` as a quoted DOT string with shared escaping.

    Only this helper may emit stored or otherwise dynamic text into DOT.
    """
    if not isinstance(value, str):
        raise SubgraphDotError("DOT string values must be str")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SubgraphDotError("DOT string is not strict UTF-8") from exc
    out: List[str] = ['"']
    for char in value:
        escaped = _CONTROL_ESCAPES.get(char)
        if escaped is not None:
            out.append(escaped)
            continue
        code = ord(char)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            out.append(f"\\x{code:02x}")
            continue
        out.append(char)
    out.append('"')
    quoted = "".join(out)
    try:
        quoted.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SubgraphDotError("DOT string is not strict UTF-8") from exc
    return quoted


def dumps_subgraph_dot(result: Mapping[str, Any]) -> str:
    """Serialize a subgraph producer result to deterministic DOT.

    The returned string is the complete payload, including the final
    newline, and is guaranteed to be at most
    :data:`HARD_MAX_SUBGRAPH_DOT_BYTES` UTF-8 bytes.
    """
    if not isinstance(result, Mapping):
        raise SubgraphDotError("subgraph DOT input must be a mapping")

    resolved = _require_bool(result.get("resolved"), "resolved")
    direction = _require_nonempty_str(result.get("direction"), "direction")
    if direction not in {"outgoing", "incoming", "both"}:
        raise SubgraphDotError(f"invalid subgraph direction {direction!r}")
    max_depth = _require_int(result.get("max_depth"), "max_depth")
    max_nodes = _require_int(result.get("max_nodes"), "max_nodes", minimum=1)
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
        raise SubgraphDotError("n_nodes_returned does not match nodes")
    if n_edges_returned != len(edges):
        raise SubgraphDotError("n_edges_returned does not match edges")
    if n_nodes_returned > n_nodes_total:
        raise SubgraphDotError("returned node count exceeds total")
    if n_edges_returned > n_edges_total:
        raise SubgraphDotError("returned edge count exceeds total")
    if n_nodes_returned > max_nodes:
        raise SubgraphDotError("returned node count exceeds max_nodes")
    if n_edges_returned > max_edges:
        raise SubgraphDotError("returned edge count exceeds max_edges")
    if nodes_truncated != (n_nodes_returned < n_nodes_total):
        raise SubgraphDotError("nodes_truncated disagrees with node counts")
    if edges_truncated != (n_edges_returned < n_edges_total):
        raise SubgraphDotError("edges_truncated disagrees with edge counts")

    root = result.get("root")
    if resolved:
        root_title = _require_nonempty_str(root, "root")
        if not nodes:
            raise SubgraphDotError("resolved subgraph is missing the root node")
    else:
        if root is not None:
            raise SubgraphDotError("unresolved subgraph must use a null root")
        if nodes or edges:
            raise SubgraphDotError("unresolved subgraph must have empty material")
        if (
            n_nodes_total != 0
            or n_edges_total != 0
            or n_nodes_returned != 0
            or n_edges_returned != 0
        ):
            raise SubgraphDotError("unresolved subgraph must have zero totals")
        root_title = None

    graph_attrs: List[Tuple[str, str]] = [
        ("schema_version", str(SUBGRAPH_DOT_SCHEMA_VERSION)),
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
            ("edge_types", _edge_types_token(result.get("edge_types"))),
            ("n_nodes_total", str(n_nodes_total)),
            ("n_edges_total", str(n_edges_total)),
            ("n_nodes_returned", str(n_nodes_returned)),
            ("n_edges_returned", str(n_edges_returned)),
            ("nodes_truncated", _dot_bool(nodes_truncated)),
            ("edges_truncated", _dot_bool(edges_truncated)),
        ]
    )

    title_to_id: dict[str, str] = {}
    node_lines: List[str] = []
    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            raise SubgraphDotError("subgraph node must be a mapping")
        title = _require_nonempty_str(node.get("title"), "node title")
        if title in title_to_id:
            raise SubgraphDotError(f"duplicate subgraph node title {title!r}")
        ident = f"n{index:04d}"
        title_to_id[title] = ident
        depth = _require_int(node.get("depth"), "node depth")
        if depth > max_depth:
            raise SubgraphDotError("node depth exceeds max_depth")
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
        first_title = _require_nonempty_str(nodes[0].get("title"), "node title")
        if first_title != root_title:
            raise SubgraphDotError("producer node order must start with the root")
        if title_to_id[root_title] != "n0000":
            raise SubgraphDotError("root must use identifier n0000")
        if _require_int(nodes[0].get("depth"), "root depth") != 0:
            raise SubgraphDotError("root node depth must be zero")

    edge_lines: List[str] = []
    seen_edge_ids: set[str] = set()
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise SubgraphDotError("subgraph edge must be a mapping")
        source = _require_nonempty_str(edge.get("source"), "edge source")
        target = _require_nonempty_str(edge.get("target"), "edge target")
        rel_type = _require_nonempty_str(edge.get("type"), "edge type")
        rel_id = _require_nonempty_str(edge.get("id"), "edge id")
        if rel_id in seen_edge_ids:
            raise SubgraphDotError(f"duplicate subgraph edge id {rel_id!r}")
        seen_edge_ids.add(rel_id)
        depth = _require_int(edge.get("depth"), "edge depth")
        if depth > max_depth:
            raise SubgraphDotError("edge depth exceeds max_depth")
        src_id = title_to_id.get(source)
        tgt_id = title_to_id.get(target)
        if src_id is None or tgt_id is None:
            raise SubgraphDotError(
                "subgraph edge endpoint is not a returned node"
            )
        attrs = [
            ("label", rel_type),
            ("id", rel_id),
            ("type", rel_type),
            ("depth", str(depth)),
        ]
        edge_lines.append(
            f"  {src_id} -> {tgt_id} [{_format_attrs(attrs)}];"
        )

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
        raise SubgraphDotError("DOT payload must be str")
    if not payload.endswith("\n") or payload.endswith("\n\n"):
        raise SubgraphDotError("DOT payload must end with exactly one newline")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SubgraphDotError("subgraph DOT is not strict UTF-8") from exc
    if b"\r" in encoded:
        raise SubgraphDotError("DOT payload must not contain raw carriage returns")
    if any(byte < 0x20 and byte != 0x0A or byte == 0x7F for byte in encoded):
        raise SubgraphDotError("DOT payload contains a raw control character")
    if len(encoded) > HARD_MAX_SUBGRAPH_DOT_BYTES:
        raise SubgraphDotError(
            f"subgraph DOT exceeds hard limit of {HARD_MAX_SUBGRAPH_DOT_BYTES} bytes"
        )
    return payload


def _format_attrs(attrs: Sequence[Tuple[str, str]]) -> str:
    return ", ".join(_attr(name, value) for name, value in attrs)


def _attr(name: str, value: str) -> str:
    if not name.isidentifier() or not name.isascii():
        raise SubgraphDotError(f"invalid DOT attribute name {name!r}")
    return f"{name}={quote_dot_string(value)}"


def _edge_types_token(raw: Any) -> str:
    if raw is None:
        return "null"
    if not isinstance(raw, list):
        raise SubgraphDotError("edge_types must be a list or null")
    if not raw:
        raise SubgraphDotError("edge_types must be non-empty when present")
    tokens: List[str] = []
    seen: set[str] = set()
    for item in raw:
        token = _require_nonempty_str(item, "edge type")
        if token.strip() != token or "\x00" in token:
            raise SubgraphDotError(f"invalid edge_types value {token!r}")
        if token in seen:
            raise SubgraphDotError(f"duplicate edge_types value {token!r}")
        seen.add(token)
        tokens.append(token)
    if tokens != sorted(tokens, key=lambda value: value.encode("utf-8")):
        raise SubgraphDotError("edge_types must use canonical UTF-8 byte order")
    return json.dumps(
        tokens,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _require_list(raw: Any, name: str) -> List[Any]:
    if not isinstance(raw, list):
        raise SubgraphDotError(f"{name} must be a list")
    return raw


def _require_bool(raw: Any, name: str) -> bool:
    if not isinstance(raw, bool):
        raise SubgraphDotError(f"{name} must be a boolean")
    return raw


def _require_int(raw: Any, name: str, *, minimum: int = 0) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise SubgraphDotError(f"{name} must be an integer")
    if raw < minimum:
        raise SubgraphDotError(f"{name} must be >= {minimum}")
    return raw


def _require_str(raw: Any, name: str) -> str:
    if not isinstance(raw, str):
        raise SubgraphDotError(f"{name} must be a string")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SubgraphDotError(f"{name} is not strict UTF-8") from exc
    return raw


def _require_nonempty_str(raw: Any, name: str) -> str:
    text = _require_str(raw, name)
    if not text:
        raise SubgraphDotError(f"{name} must be a non-empty string")
    return text


def _dot_bool(value: bool) -> str:
    return "true" if value else "false"
