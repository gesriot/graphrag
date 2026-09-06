"""Deterministic Graphviz DOT serialization for a bounded components result.

This module renders an already-computed ``ByogGraph.components`` /
``compute_weakly_connected_components`` mapping. It does not open a graph,
inspect a snapshot, resolve endpoints, traverse or reconstruct
relationships, invoke Graphviz, spawn subprocesses, touch the network, or
write temporary files. It does not sort, expand, truncate, or otherwise
modify producer material. The producer does not return individual
relationship rows, so this renderer emits no edge statements and does not
invent edges. Component ``n_edges_total`` values are metadata only.

DOT here is a visualization **interchange** format on stdout. The project
does not render an image and does not provide an interactive UI.

Contract
========

* Undirected, non-strict ``graph graphrag_components``.
* Each returned component is one Graphviz cluster in **producer order**:
  ``cluster_c0000``, ``cluster_c0001``, … Internal component ids are
  ``c0000``, ``c0001``, … Raw representatives are never identifiers.
* Cluster ``label`` is the exact representative.
* Returned node titles are rendered inside their cluster with globally
  unique internal ids ``n0000``, ``n0001``, … in flattened producer
  component/node order. Raw titles are never identifiers.
* Node statements use exact stored titles for ``label`` and ``title``.
  The producer does not provide per-node entity/endpoint-only status, so
  this renderer does not infer it.
* No relationship-edge statements. ``n_edges_total`` is metadata, not
  rendered edge material.
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
  community, or semantic meaning.
* Hard limit: :data:`HARD_MAX_COMPONENTS_DOT_BYTES` UTF-8 bytes for the
  complete payload, including the final newline. Overflow fails before
  any caller write. The successful payload ends with exactly one newline.

An empty producer result (zero components, zero totals, false truncation
flags) renders as a valid empty graph with graph-level metadata and no
clusters or nodes. That is not an error. Truncation remains visible in
metadata and is not reconstructed as extra material.
"""

from __future__ import annotations

import json
from typing import Any, List, Mapping, Sequence, Tuple

from .subgraph_dot import SubgraphDotError, quote_dot_string

COMPONENTS_DOT_SCHEMA_VERSION = 1
HARD_MAX_COMPONENTS_DOT_BYTES = 1_000_000
_GRAPH_NAME = "graphrag_components"
# Schema-1 producer caps. Kept local so this serializer stays independent of
# graph loading; regression tests exercise the matching byog_graph limits.
_HARD_MAX_COMPONENTS = 100
_HARD_MAX_NODES_PER_COMPONENT = 100

_REQUIRED_TOP = (
    "edge_types",
    "max_components",
    "max_nodes_per_component",
    "components",
    "n_components_total",
    "n_components_returned",
    "n_nodes_total",
    "n_edges_total",
    "n_entity_nodes_total",
    "n_endpoint_only_nodes_total",
    "components_truncated",
    "nodes_truncated",
)
_REQUIRED_COMPONENT = (
    "representative",
    "nodes",
    "n_nodes_total",
    "n_edges_total",
    "n_nodes_returned",
    "n_entity_nodes",
    "n_endpoint_only_nodes",
    "nodes_truncated",
)


class ComponentsDotError(ValueError):
    """Invalid components DOT input, unencodable value, or byte-limit overflow."""


def dumps_components_dot(result: Mapping[str, Any]) -> str:
    """Serialize a weakly-connected-components producer result to deterministic DOT.

    The returned string is the complete payload, including the final
    newline, and is guaranteed to be at most
    :data:`HARD_MAX_COMPONENTS_DOT_BYTES` UTF-8 bytes.
    """
    try:
        return _dumps_components_dot(result)
    except SubgraphDotError as exc:
        raise ComponentsDotError(str(exc)) from exc


def _dumps_components_dot(result: Mapping[str, Any]) -> str:
    if not isinstance(result, Mapping):
        raise ComponentsDotError("components DOT input must be a mapping")
    for name in _REQUIRED_TOP:
        if name not in result:
            raise ComponentsDotError(f"missing components field {name}")

    max_components = _require_int(
        result.get("max_components"),
        "max_components",
        minimum=1,
        maximum=_HARD_MAX_COMPONENTS,
    )
    max_nodes_per_component = _require_int(
        result.get("max_nodes_per_component"),
        "max_nodes_per_component",
        minimum=1,
        maximum=_HARD_MAX_NODES_PER_COMPONENT,
    )
    n_components_total = _require_int(
        result.get("n_components_total"), "n_components_total"
    )
    n_components_returned = _require_int(
        result.get("n_components_returned"), "n_components_returned"
    )
    n_nodes_total = _require_int(result.get("n_nodes_total"), "n_nodes_total")
    n_edges_total = _require_int(result.get("n_edges_total"), "n_edges_total")
    n_entity_nodes_total = _require_int(
        result.get("n_entity_nodes_total"), "n_entity_nodes_total"
    )
    n_endpoint_only_nodes_total = _require_int(
        result.get("n_endpoint_only_nodes_total"),
        "n_endpoint_only_nodes_total",
    )
    components_truncated = _require_bool(
        result.get("components_truncated"), "components_truncated"
    )
    nodes_truncated = _require_bool(result.get("nodes_truncated"), "nodes_truncated")
    components = _require_list(result.get("components"), "components")
    edge_types_token = _edge_types_token(result.get("edge_types"))

    if n_components_returned != len(components):
        raise ComponentsDotError("n_components_returned does not match components")
    if n_components_returned > n_components_total:
        raise ComponentsDotError("returned component count exceeds total")
    if n_components_returned > max_components:
        raise ComponentsDotError("returned component count exceeds max_components")
    if components_truncated != (n_components_returned < n_components_total):
        raise ComponentsDotError(
            "components_truncated disagrees with component counts"
        )
    if components_truncated and n_components_returned != max_components:
        raise ComponentsDotError(
            "truncated components must return exactly max_components"
        )
    if (not components_truncated) and n_components_returned != n_components_total:
        raise ComponentsDotError(
            "untruncated components must return every component"
        )
    if n_entity_nodes_total + n_endpoint_only_nodes_total != n_nodes_total:
        raise ComponentsDotError(
            "entity and endpoint-only counts do not cover the node universe"
        )
    if n_components_total > 0 and n_nodes_total < n_components_total:
        raise ComponentsDotError("node total is smaller than component total")
    if n_components_total == 0:
        _require_empty_result(
            n_components_returned=n_components_returned,
            n_nodes_total=n_nodes_total,
            n_edges_total=n_edges_total,
            n_entity_nodes_total=n_entity_nodes_total,
            n_endpoint_only_nodes_total=n_endpoint_only_nodes_total,
            components_truncated=components_truncated,
            nodes_truncated=nodes_truncated,
            components=components,
        )

    graph_attrs: List[Tuple[str, str]] = [
        ("schema_version", str(COMPONENTS_DOT_SCHEMA_VERSION)),
        ("edge_types", edge_types_token),
        ("max_components", str(max_components)),
        ("max_nodes_per_component", str(max_nodes_per_component)),
        ("n_components_total", str(n_components_total)),
        ("n_components_returned", str(n_components_returned)),
        ("n_nodes_total", str(n_nodes_total)),
        ("n_edges_total", str(n_edges_total)),
        ("n_entity_nodes_total", str(n_entity_nodes_total)),
        ("n_endpoint_only_nodes_total", str(n_endpoint_only_nodes_total)),
        ("components_truncated", _dot_bool(components_truncated)),
        ("nodes_truncated", _dot_bool(nodes_truncated)),
    ]

    cluster_blocks: List[str] = []
    seen_titles: set[str] = set()
    seen_representatives: set[str] = set()
    previous_sort_key: Tuple[int, int, bytes] | None = None
    returned_nodes_total = 0
    returned_edges_total = 0
    returned_entity = 0
    returned_endpoint_only = 0
    returned_node_titles = 0
    any_component_nodes_truncated = False
    node_index = 0

    for index, component in enumerate(components):
        if not isinstance(component, Mapping):
            raise ComponentsDotError("components component must be a mapping")
        for name in _REQUIRED_COMPONENT:
            if name not in component:
                raise ComponentsDotError(f"missing component field {name}")
        representative = _require_nonempty_str(
            component.get("representative"), "component representative"
        )
        if representative in seen_representatives:
            raise ComponentsDotError(
                f"duplicate components representative {representative!r}"
            )
        seen_representatives.add(representative)
        nodes = _require_str_list(component.get("nodes"), "component nodes")
        n_comp_nodes_total = _require_int(
            component.get("n_nodes_total"), "component n_nodes_total", minimum=1
        )
        n_comp_edges_total = _require_int(
            component.get("n_edges_total"), "component n_edges_total"
        )
        n_comp_nodes_returned = _require_int(
            component.get("n_nodes_returned"),
            "component n_nodes_returned",
            minimum=1,
        )
        n_entity = _require_int(
            component.get("n_entity_nodes"), "component n_entity_nodes"
        )
        n_endpoint_only = _require_int(
            component.get("n_endpoint_only_nodes"),
            "component n_endpoint_only_nodes",
        )
        comp_nodes_truncated = _require_bool(
            component.get("nodes_truncated"), "component nodes_truncated"
        )
        if n_comp_nodes_returned != len(nodes):
            raise ComponentsDotError(
                "n_nodes_returned does not match component nodes"
            )
        if n_comp_nodes_returned > n_comp_nodes_total:
            raise ComponentsDotError(
                "returned component node count exceeds component total"
            )
        if n_comp_nodes_returned > max_nodes_per_component:
            raise ComponentsDotError(
                "returned component node count exceeds max_nodes_per_component"
            )
        if comp_nodes_truncated != (n_comp_nodes_returned < n_comp_nodes_total):
            raise ComponentsDotError(
                "component nodes_truncated disagrees with node counts"
            )
        if comp_nodes_truncated and n_comp_nodes_returned != max_nodes_per_component:
            raise ComponentsDotError(
                "truncated component nodes must return exactly max_nodes_per_component"
            )
        if (not comp_nodes_truncated) and n_comp_nodes_returned != n_comp_nodes_total:
            raise ComponentsDotError(
                "untruncated component must return every member"
            )
        if n_entity + n_endpoint_only != n_comp_nodes_total:
            raise ComponentsDotError(
                "component entity and endpoint-only counts do not cover members"
            )
        if nodes != sorted(nodes, key=lambda title: title.encode("utf-8")):
            raise ComponentsDotError(
                "component nodes must use canonical UTF-8 byte order"
            )
        if nodes[0] != representative:
            raise ComponentsDotError(
                "component representative must be the first returned node"
            )
        sort_key = (
            -n_comp_nodes_total,
            -n_comp_edges_total,
            representative.encode("utf-8"),
        )
        if previous_sort_key is not None and sort_key <= previous_sort_key:
            raise ComponentsDotError(
                "components are not in canonical producer order"
            )
        previous_sort_key = sort_key
        for title in nodes:
            if title in seen_titles:
                raise ComponentsDotError(
                    f"duplicate components node title {title!r}"
                )
            seen_titles.add(title)

        ident = f"c{index:04d}"
        cluster_attrs: List[Tuple[str, str]] = [
            ("label", representative),
            ("component_id", ident),
            ("representative", representative),
            ("n_nodes_total", str(n_comp_nodes_total)),
            ("n_edges_total", str(n_comp_edges_total)),
            ("n_nodes_returned", str(n_comp_nodes_returned)),
            ("n_entity_nodes", str(n_entity)),
            ("n_endpoint_only_nodes", str(n_endpoint_only)),
            ("nodes_truncated", _dot_bool(comp_nodes_truncated)),
        ]
        lines = [f"  subgraph cluster_{ident} {{"]
        for name, value in cluster_attrs:
            lines.append(f"    {_attr(name, value)};")
        for title in nodes:
            node_ident = f"n{node_index:04d}"
            node_index += 1
            node_attrs = [("label", title), ("title", title)]
            lines.append(f"    {node_ident} [{_format_attrs(node_attrs)}];")
        lines.append("  }")
        cluster_blocks.extend(lines)

        returned_nodes_total += n_comp_nodes_total
        returned_edges_total += n_comp_edges_total
        returned_entity += n_entity
        returned_endpoint_only += n_endpoint_only
        returned_node_titles += n_comp_nodes_returned
        if comp_nodes_truncated:
            any_component_nodes_truncated = True

    if nodes_truncated != any_component_nodes_truncated:
        raise ComponentsDotError(
            "nodes_truncated disagrees with returned component flags"
        )
    if returned_nodes_total > n_nodes_total:
        raise ComponentsDotError(
            "returned component node totals exceed n_nodes_total"
        )
    if returned_edges_total > n_edges_total:
        raise ComponentsDotError(
            "returned component edge totals exceed n_edges_total"
        )
    if returned_entity > n_entity_nodes_total:
        raise ComponentsDotError(
            "returned entity nodes exceed n_entity_nodes_total"
        )
    if returned_endpoint_only > n_endpoint_only_nodes_total:
        raise ComponentsDotError(
            "returned endpoint-only nodes exceed n_endpoint_only_nodes_total"
        )
    if returned_node_titles > n_nodes_total:
        raise ComponentsDotError(
            "returned node titles exceed n_nodes_total"
        )
    omitted_components = n_components_total - n_components_returned
    if n_nodes_total - returned_nodes_total < omitted_components:
        raise ComponentsDotError(
            "node total cannot provide one member for each omitted component"
        )
    if not components_truncated:
        if returned_nodes_total != n_nodes_total:
            raise ComponentsDotError(
                "untruncated components must cover n_nodes_total"
            )
        if returned_edges_total != n_edges_total:
            raise ComponentsDotError(
                "untruncated components must cover n_edges_total"
            )
        if returned_entity != n_entity_nodes_total:
            raise ComponentsDotError(
                "untruncated components must cover n_entity_nodes_total"
            )
        if returned_endpoint_only != n_endpoint_only_nodes_total:
            raise ComponentsDotError(
                "untruncated components must cover n_endpoint_only_nodes_total"
            )

    lines: List[str] = [f"graph {_GRAPH_NAME} {{"]
    lines.append("  graph [")
    last = len(graph_attrs) - 1
    for index, (name, value) in enumerate(graph_attrs):
        suffix = "," if index != last else ""
        lines.append(f"    {_attr(name, value)}{suffix}")
    lines.append("  ];")
    lines.append(f"  {_attr('rankdir', 'LR')};")
    lines.append(f"  node [{_attr('shape', 'box')}];")
    lines.extend(cluster_blocks)
    lines.append("}")
    payload = "\n".join(lines) + "\n"
    return _checked_payload(payload)


def _require_empty_result(
    *,
    n_components_returned: int,
    n_nodes_total: int,
    n_edges_total: int,
    n_entity_nodes_total: int,
    n_endpoint_only_nodes_total: int,
    components_truncated: bool,
    nodes_truncated: bool,
    components: List[Any],
) -> None:
    if components:
        raise ComponentsDotError("empty components must have empty material")
    zeros = (
        n_components_returned,
        n_nodes_total,
        n_edges_total,
        n_entity_nodes_total,
        n_endpoint_only_nodes_total,
    )
    if any(value != 0 for value in zeros):
        raise ComponentsDotError("empty components must have zero totals")
    if components_truncated or nodes_truncated:
        raise ComponentsDotError("empty components must not be truncated")


def _checked_payload(payload: str) -> str:
    if not isinstance(payload, str):
        raise ComponentsDotError("DOT payload must be str")
    if not payload.endswith("\n") or payload.endswith("\n\n"):
        raise ComponentsDotError("DOT payload must end with exactly one newline")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ComponentsDotError("components DOT is not strict UTF-8") from exc
    if b"\r" in encoded:
        raise ComponentsDotError("DOT payload must not contain raw carriage returns")
    if any(byte < 0x20 and byte != 0x0A or byte == 0x7F for byte in encoded):
        raise ComponentsDotError("DOT payload contains a raw control character")
    if len(encoded) > HARD_MAX_COMPONENTS_DOT_BYTES:
        raise ComponentsDotError(
            f"components DOT exceeds hard limit of {HARD_MAX_COMPONENTS_DOT_BYTES} bytes"
        )
    return payload


def _format_attrs(attrs: Sequence[Tuple[str, str]]) -> str:
    return ", ".join(_attr(name, value) for name, value in attrs)


def _attr(name: str, value: str) -> str:
    if not name.isidentifier() or not name.isascii():
        raise ComponentsDotError(f"invalid DOT attribute name {name!r}")
    return f"{name}={_quote(value)}"


def _quote(value: str) -> str:
    try:
        return quote_dot_string(value)
    except SubgraphDotError as exc:
        raise ComponentsDotError(str(exc)) from exc


def _edge_types_token(raw: Any) -> str:
    if raw is None:
        return "null"
    if not isinstance(raw, list):
        raise ComponentsDotError("edge_types must be a list or null")
    if not raw:
        raise ComponentsDotError("edge_types must be non-empty when present")
    tokens: List[str] = []
    seen: set[str] = set()
    for item in raw:
        token = _require_nonempty_str(item, "edge type")
        if token.strip() != token or "\x00" in token:
            raise ComponentsDotError(f"invalid edge_types value {token!r}")
        if token in seen:
            raise ComponentsDotError(f"duplicate edge_types value {token!r}")
        seen.add(token)
        tokens.append(token)
    if tokens != sorted(tokens, key=lambda value: value.encode("utf-8")):
        raise ComponentsDotError("edge_types must use canonical UTF-8 byte order")
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
        raise ComponentsDotError("canonical JSON token is invalid") from exc


def _require_list(raw: Any, name: str) -> List[Any]:
    if not isinstance(raw, list):
        raise ComponentsDotError(f"{name} must be a list")
    return raw


def _require_str_list(raw: Any, name: str) -> List[str]:
    items = _require_list(raw, name)
    titles: List[str] = []
    seen: set[str] = set()
    for item in items:
        title = _require_nonempty_str(item, name)
        if title in seen:
            raise ComponentsDotError(f"duplicate {name} value {title!r}")
        seen.add(title)
        titles.append(title)
    return titles


def _require_bool(raw: Any, name: str) -> bool:
    if not isinstance(raw, bool):
        raise ComponentsDotError(f"{name} must be a boolean")
    return raw


def _require_int(
    raw: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ComponentsDotError(f"{name} must be an integer")
    if raw < minimum:
        raise ComponentsDotError(f"{name} must be >= {minimum}")
    if maximum is not None and raw > maximum:
        raise ComponentsDotError(f"{name} must be <= {maximum}")
    return raw


def _require_str(raw: Any, name: str) -> str:
    if not isinstance(raw, str):
        raise ComponentsDotError(f"{name} must be a string")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ComponentsDotError(f"{name} is not strict UTF-8") from exc
    return raw


def _require_nonempty_str(raw: Any, name: str) -> str:
    text = _require_str(raw, name)
    if not text:
        raise ComponentsDotError(f"{name} must be a non-empty string")
    return text


def _dot_bool(value: bool) -> str:
    return "true" if value else "false"
