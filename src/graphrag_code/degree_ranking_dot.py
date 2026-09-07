"""Deterministic Graphviz DOT serialization for a bounded degree-ranking result.

This module renders an already-computed ``ByogGraph.degree_ranking`` /
``compute_structural_degree_ranking`` mapping. It does not open a graph,
inspect a snapshot, resolve endpoints, reread entities or relationship
rows, reconstruct topology, invoke Graphviz, spawn subprocesses, touch
the network, or write temporary files. It does not sort, expand,
truncate, or otherwise modify producer material. The producer does not
return individual relationship rows, so this renderer emits no edge
statements and does not invent, infer, or reconstruct edges. Degree
sums and ``n_edges_total`` are metadata only.

DOT here is a visualization **interchange** format on stdout. The project
does not render an image and does not provide an interactive UI.
Graphviz is not invoked. Statement order follows producer ranking
order; that is not a claim that a rendered Graphviz layout will
preserve that order. Internal identifiers ``n0000``, ``n0001``, … are
serialization identifiers only: they are not an ordinal rank field or a
semantic score.

Contract
========

* Directed, non-strict ``digraph graphrag_degree_ranking``.
* Each returned ranking row is one node statement in **producer order**.
  Internal identifiers are ``n0000``, ``n0001``, … Raw titles are never
  identifiers.
* Node attributes, in this exact order: ``label``, ``title``,
  ``in_degree``, ``out_degree``, ``total_degree``, ``is_entity``.
* No relationship-edge, invisible, layout, or reconstructed edge
  statements. No rank constraints are added to force presentation order.
* The graph-level ``edge_types`` value is canonical JSON text: ``null``
  means no filter; a JSON array preserves exact item boundaries
  (including commas and a literal ``"all"`` relationship type).
* Dynamic strings are emitted only as quoted DOT strings through
  :func:`graphrag_code.subgraph_dot.quote_dot_string`. No stored value is
  interpolated into an identifier, raw attribute, or comment.
* Control characters never appear raw. Invalid strict-UTF-8 strings,
  including lone surrogates, fail closed.
* Presentation-only attributes ``rankdir=LR`` and ``shape=box`` are
  fixed. They do not imply importance, ownership, architecture,
  leadership, severity, or semantic meaning. Nodes are not styled by
  degree or entity status.
* Hard limit: :data:`HARD_MAX_DEGREE_RANKING_DOT_BYTES` UTF-8 bytes
  for the complete payload, including the final newline. Overflow fails
  before any caller write. The successful payload ends with exactly one
  newline.

An empty producer result (zero nodes, zero totals, false truncation)
renders as a valid empty digraph with graph-level metadata and no nodes.
That is not an error. Truncation remains visible in metadata and is not
reconstructed as extra material.

This is raw directed relationship-row degree accounting: not PageRank,
betweenness, closeness, eigenvector centrality, a normalized score,
semantic importance, leadership, architecture, hierarchy, community
detection, GraphRAG, or natural-language analysis. Self-loops contribute
incoming 1, outgoing 1, total 2. Parallel rows each count.
"""

from __future__ import annotations

import json
from typing import Any, List, Mapping, Sequence, Tuple

from .subgraph_dot import SubgraphDotError, quote_dot_string

DEGREE_RANKING_DOT_SCHEMA_VERSION = 1
HARD_MAX_DEGREE_RANKING_DOT_BYTES = 1_000_000
_GRAPH_NAME = "graphrag_degree_ranking"
# Schema-1 producer cap. Kept local so this serializer stays independent of
# graph loading; regression tests exercise the matching byog_graph limit.
_HARD_MAX_NODES = 100
_RANK_BY_MODES = ("total", "incoming", "outgoing")
_REQUIRED_TOP = (
    "rank_by",
    "edge_types",
    "max_nodes",
    "nodes",
    "n_nodes_total",
    "n_nodes_returned",
    "n_edges_total",
    "n_entity_nodes_total",
    "n_endpoint_only_nodes_total",
    "sum_in_degree",
    "sum_out_degree",
    "sum_total_degree",
    "nodes_truncated",
)
_REQUIRED_NODE = (
    "title",
    "in_degree",
    "out_degree",
    "total_degree",
    "is_entity",
)


class DegreeRankingDotError(ValueError):
    """Invalid degree-ranking DOT input, unencodable value, or overflow."""


def dumps_degree_ranking_dot(result: Mapping[str, Any]) -> str:
    """Serialize a degree-ranking producer result to deterministic DOT.

    The returned string is the complete payload, including the final
    newline, and is guaranteed to be at most
    :data:`HARD_MAX_DEGREE_RANKING_DOT_BYTES` UTF-8 bytes.
    """
    try:
        return _dumps_degree_ranking_dot(result)
    except SubgraphDotError as exc:
        raise DegreeRankingDotError(str(exc)) from exc


def _dumps_degree_ranking_dot(result: Mapping[str, Any]) -> str:
    if not isinstance(result, Mapping):
        raise DegreeRankingDotError("degree-ranking DOT input must be a mapping")
    for name in _REQUIRED_TOP:
        if name not in result:
            raise DegreeRankingDotError(f"missing degree-ranking field {name}")

    rank_by = result.get("rank_by")
    if not isinstance(rank_by, str) or rank_by not in _RANK_BY_MODES:
        raise DegreeRankingDotError(f"invalid degree-ranking rank_by {rank_by!r}")
    max_nodes = _require_int(
        result.get("max_nodes"),
        "max_nodes",
        minimum=1,
        maximum=_HARD_MAX_NODES,
    )
    n_nodes_total = _require_int(result.get("n_nodes_total"), "n_nodes_total")
    n_nodes_returned = _require_int(
        result.get("n_nodes_returned"), "n_nodes_returned"
    )
    n_edges_total = _require_int(result.get("n_edges_total"), "n_edges_total")
    n_entity_nodes_total = _require_int(
        result.get("n_entity_nodes_total"), "n_entity_nodes_total"
    )
    n_endpoint_only_nodes_total = _require_int(
        result.get("n_endpoint_only_nodes_total"),
        "n_endpoint_only_nodes_total",
    )
    sum_in_degree = _require_int(result.get("sum_in_degree"), "sum_in_degree")
    sum_out_degree = _require_int(result.get("sum_out_degree"), "sum_out_degree")
    sum_total_degree = _require_int(
        result.get("sum_total_degree"), "sum_total_degree"
    )
    nodes_truncated = _require_bool(result.get("nodes_truncated"), "nodes_truncated")
    nodes = _require_list(result.get("nodes"), "nodes")
    edge_types_token = _edge_types_token(result.get("edge_types"))

    if n_nodes_returned != len(nodes):
        raise DegreeRankingDotError("n_nodes_returned does not match nodes")
    if n_nodes_returned > n_nodes_total:
        raise DegreeRankingDotError("returned node count exceeds total")
    if n_nodes_returned > max_nodes:
        raise DegreeRankingDotError("returned node count exceeds max_nodes")
    if nodes_truncated != (n_nodes_returned < n_nodes_total):
        raise DegreeRankingDotError("nodes_truncated disagrees with node counts")
    if nodes_truncated and n_nodes_returned != max_nodes:
        raise DegreeRankingDotError("truncated ranking must return exactly max_nodes")
    if (not nodes_truncated) and n_nodes_returned != n_nodes_total:
        raise DegreeRankingDotError("untruncated ranking must return every node")
    if n_entity_nodes_total + n_endpoint_only_nodes_total != n_nodes_total:
        raise DegreeRankingDotError(
            "entity and endpoint-only counts do not cover the node universe"
        )
    if sum_in_degree != n_edges_total:
        raise DegreeRankingDotError("sum_in_degree does not equal n_edges_total")
    if sum_out_degree != n_edges_total:
        raise DegreeRankingDotError("sum_out_degree does not equal n_edges_total")
    if sum_total_degree != 2 * n_edges_total:
        raise DegreeRankingDotError(
            "sum_total_degree does not equal twice n_edges_total"
        )
    if n_nodes_total == 0:
        _require_empty_result(
            n_nodes_returned=n_nodes_returned,
            n_edges_total=n_edges_total,
            n_entity_nodes_total=n_entity_nodes_total,
            n_endpoint_only_nodes_total=n_endpoint_only_nodes_total,
            sum_in_degree=sum_in_degree,
            sum_out_degree=sum_out_degree,
            sum_total_degree=sum_total_degree,
            nodes_truncated=nodes_truncated,
            nodes=nodes,
        )

    graph_attrs: List[Tuple[str, str]] = [
        ("schema_version", str(DEGREE_RANKING_DOT_SCHEMA_VERSION)),
        ("rank_by", rank_by),
        ("edge_types", edge_types_token),
        ("max_nodes", str(max_nodes)),
        ("n_nodes_total", str(n_nodes_total)),
        ("n_nodes_returned", str(n_nodes_returned)),
        ("n_edges_total", str(n_edges_total)),
        ("n_entity_nodes_total", str(n_entity_nodes_total)),
        ("n_endpoint_only_nodes_total", str(n_endpoint_only_nodes_total)),
        ("sum_in_degree", str(sum_in_degree)),
        ("sum_out_degree", str(sum_out_degree)),
        ("sum_total_degree", str(sum_total_degree)),
        ("nodes_truncated", _dot_bool(nodes_truncated)),
    ]

    node_lines: List[str] = []
    seen_titles: set[str] = set()
    previous_sort_key: Tuple[int, int, int, bytes] | None = None
    returned_in = 0
    returned_out = 0
    returned_total = 0
    returned_entity = 0
    returned_endpoint_only = 0

    for index, node in enumerate(nodes):
        if not isinstance(node, Mapping):
            raise DegreeRankingDotError("degree-ranking node must be a mapping")
        for name in _REQUIRED_NODE:
            if name not in node:
                raise DegreeRankingDotError(f"missing node field {name}")
        title = _require_nonempty_str(node.get("title"), "node title")
        if title in seen_titles:
            raise DegreeRankingDotError(
                f"duplicate degree-ranking node title {title!r}"
            )
        seen_titles.add(title)
        in_degree = _require_int(node.get("in_degree"), "node in_degree")
        out_degree = _require_int(node.get("out_degree"), "node out_degree")
        total_degree = _require_int(node.get("total_degree"), "node total_degree")
        is_entity = _require_bool(node.get("is_entity"), "node is_entity")
        if total_degree != in_degree + out_degree:
            raise DegreeRankingDotError(
                "node total_degree does not equal in_degree + out_degree"
            )
        sort_key = _ranking_sort_key(
            rank_by, in_degree, out_degree, total_degree, title
        )
        if previous_sort_key is not None and sort_key <= previous_sort_key:
            raise DegreeRankingDotError("nodes are not in canonical producer order")
        previous_sort_key = sort_key
        ident = f"n{index:04d}"
        node_attrs: List[Tuple[str, str]] = [
            ("label", title),
            ("title", title),
            ("in_degree", str(in_degree)),
            ("out_degree", str(out_degree)),
            ("total_degree", str(total_degree)),
            ("is_entity", _dot_bool(is_entity)),
        ]
        node_lines.append(f"  {ident} [{_format_attrs(node_attrs)}];")
        returned_in += in_degree
        returned_out += out_degree
        returned_total += total_degree
        if is_entity:
            returned_entity += 1
        else:
            returned_endpoint_only += 1

    if returned_in > sum_in_degree:
        raise DegreeRankingDotError("returned in-degree sum exceeds sum_in_degree")
    if returned_out > sum_out_degree:
        raise DegreeRankingDotError("returned out-degree sum exceeds sum_out_degree")
    if returned_total > sum_total_degree:
        raise DegreeRankingDotError(
            "returned total-degree sum exceeds sum_total_degree"
        )
    if returned_entity > n_entity_nodes_total:
        raise DegreeRankingDotError("returned entity nodes exceed n_entity_nodes_total")
    if returned_endpoint_only > n_endpoint_only_nodes_total:
        raise DegreeRankingDotError(
            "returned endpoint-only nodes exceed n_endpoint_only_nodes_total"
        )
    if not nodes_truncated:
        if returned_entity != n_entity_nodes_total:
            raise DegreeRankingDotError(
                "untruncated ranking must cover n_entity_nodes_total"
            )
        if returned_endpoint_only != n_endpoint_only_nodes_total:
            raise DegreeRankingDotError(
                "untruncated ranking must cover n_endpoint_only_nodes_total"
            )
        if returned_in != sum_in_degree:
            raise DegreeRankingDotError("untruncated ranking must cover sum_in_degree")
        if returned_out != sum_out_degree:
            raise DegreeRankingDotError("untruncated ranking must cover sum_out_degree")
        if returned_total != sum_total_degree:
            raise DegreeRankingDotError(
                "untruncated ranking must cover sum_total_degree"
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
    lines.append("}")
    payload = "\n".join(lines) + "\n"
    return _checked_payload(payload)


def _ranking_sort_key(
    rank_by: str,
    in_degree: int,
    out_degree: int,
    total_degree: int,
    title: str,
) -> Tuple[int, int, int, bytes]:
    title_key = title.encode("utf-8")
    incoming = -in_degree
    outgoing = -out_degree
    total = -total_degree
    if rank_by == "incoming":
        return (incoming, total, outgoing, title_key)
    if rank_by == "outgoing":
        return (outgoing, total, incoming, title_key)
    return (total, incoming, outgoing, title_key)


def _require_empty_result(
    *,
    n_nodes_returned: int,
    n_edges_total: int,
    n_entity_nodes_total: int,
    n_endpoint_only_nodes_total: int,
    sum_in_degree: int,
    sum_out_degree: int,
    sum_total_degree: int,
    nodes_truncated: bool,
    nodes: List[Any],
) -> None:
    if nodes:
        raise DegreeRankingDotError("empty degree-ranking must have empty material")
    zeros = (
        n_nodes_returned,
        n_edges_total,
        n_entity_nodes_total,
        n_endpoint_only_nodes_total,
        sum_in_degree,
        sum_out_degree,
        sum_total_degree,
    )
    if any(value != 0 for value in zeros):
        raise DegreeRankingDotError("empty degree-ranking must have zero totals")
    if nodes_truncated:
        raise DegreeRankingDotError("empty degree-ranking must not be truncated")


def _checked_payload(payload: str) -> str:
    if not isinstance(payload, str):
        raise DegreeRankingDotError("DOT payload must be str")
    if not payload.endswith("\n") or payload.endswith("\n\n"):
        raise DegreeRankingDotError(
            "DOT payload must end with exactly one newline"
        )
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DegreeRankingDotError("degree-ranking DOT is not strict UTF-8") from exc
    if b"\r" in encoded:
        raise DegreeRankingDotError(
            "DOT payload must not contain raw carriage returns"
        )
    if any(byte < 0x20 and byte != 0x0A or byte == 0x7F for byte in encoded):
        raise DegreeRankingDotError("DOT payload contains a raw control character")
    if len(encoded) > HARD_MAX_DEGREE_RANKING_DOT_BYTES:
        raise DegreeRankingDotError(
            "degree-ranking DOT exceeds hard limit of "
            f"{HARD_MAX_DEGREE_RANKING_DOT_BYTES} bytes"
        )
    return payload


def _format_attrs(attrs: Sequence[Tuple[str, str]]) -> str:
    return ", ".join(_attr(name, value) for name, value in attrs)


def _attr(name: str, value: str) -> str:
    if not name.isidentifier() or not name.isascii():
        raise DegreeRankingDotError(f"invalid DOT attribute name {name!r}")
    return f"{name}={_quote(value)}"


def _quote(value: str) -> str:
    try:
        return quote_dot_string(value)
    except SubgraphDotError as exc:
        raise DegreeRankingDotError(str(exc)) from exc


def _edge_types_token(raw: Any) -> str:
    if raw is None:
        return "null"
    if not isinstance(raw, list):
        raise DegreeRankingDotError("edge_types must be a list or null")
    if not raw:
        raise DegreeRankingDotError("edge_types must be non-empty when present")
    tokens: List[str] = []
    seen: set[str] = set()
    for item in raw:
        token = _require_nonempty_str(item, "edge type")
        if token.strip() != token or "\x00" in token:
            raise DegreeRankingDotError(f"invalid edge_types value {token!r}")
        if token in seen:
            raise DegreeRankingDotError(f"duplicate edge_types value {token!r}")
        seen.add(token)
        tokens.append(token)
    if tokens != sorted(tokens, key=lambda value: value.encode("utf-8")):
        raise DegreeRankingDotError(
            "edge_types must use canonical UTF-8 byte order"
        )
    return _json_array(tokens)


def _json_array(values: Sequence[str]) -> str:
    try:
        return json.dumps(
            list(values),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise DegreeRankingDotError("canonical JSON token is invalid") from exc


def _require_list(raw: Any, name: str) -> List[Any]:
    if not isinstance(raw, list):
        raise DegreeRankingDotError(f"{name} must be a list")
    return raw


def _require_bool(raw: Any, name: str) -> bool:
    if not isinstance(raw, bool):
        raise DegreeRankingDotError(f"{name} must be a boolean")
    return raw


def _require_int(
    raw: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise DegreeRankingDotError(f"{name} must be an integer")
    if raw < minimum:
        raise DegreeRankingDotError(f"{name} must be >= {minimum}")
    if maximum is not None and raw > maximum:
        raise DegreeRankingDotError(f"{name} must be <= {maximum}")
    return raw


def _require_str(raw: Any, name: str) -> str:
    if not isinstance(raw, str):
        raise DegreeRankingDotError(f"{name} must be a string")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DegreeRankingDotError(f"{name} is not strict UTF-8") from exc
    return raw


def _require_nonempty_str(raw: Any, name: str) -> str:
    text = _require_str(raw, name)
    if not text:
        raise DegreeRankingDotError(f"{name} must be a non-empty string")
    return text


def _dot_bool(value: bool) -> str:
    return "true" if value else "false"
