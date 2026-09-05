"""Deterministic Graphviz DOT serialization for a bounded condensation result.

This module renders an already-computed ``ByogGraph.condensation`` /
``compute_condensation_graph`` mapping. It does not open a graph, traverse
relationships, invoke Graphviz, spawn subprocesses, touch the network, or
write temporary files. It does not reconstruct, expand, sort, or truncate
the producer result.

DOT here is a visualization **interchange** format on stdout. The project
does not render an image and does not provide an interactive UI.

Contract
========

* Directed, non-strict ``digraph graphrag_condensation``.
* Internal component identifiers are ``c0000``, ``c0001``, … in **producer
  component order**. Raw representatives and node titles are never
  identifiers.
* Component and edge statements preserve producer order. This renderer
  does not sort or truncate.
* Every returned edge names the internal identifiers of its returned
  source and target components, resolved by exact representative. A
  missing or duplicate representative is a renderer error, not an
  omitted statement.
* Edges keep stored source-to-target orientation. Parallel relationship
  rows stay aggregated exactly as ``n_relationship_rows_total``.
* The graph-level ``edge_types`` value and each component ``nodes`` list
  are canonical JSON text: ``null`` means no filter; a JSON array
  preserves exact item boundaries (including commas and a literal
  ``"all"`` relationship type).
* Dynamic strings are emitted only as quoted DOT strings through
  :func:`graphrag_code.subgraph_dot.quote_dot_string`. No stored value is
  interpolated into an identifier, raw attribute, or comment.
* Control characters never appear raw. Invalid strict-UTF-8 strings,
  including lone surrogates, fail closed.
* Hard limit: :data:`HARD_MAX_CONDENSATION_DOT_BYTES` UTF-8 bytes for the
  complete payload, including the final newline. Overflow fails before
  any caller write. The successful payload ends with exactly one newline.

An empty producer result (zero components, zero edges, zero totals)
renders as a valid empty digraph. That is not an error.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .subgraph_dot import SubgraphDotError, quote_dot_string

CONDENSATION_DOT_SCHEMA_VERSION = 1
HARD_MAX_CONDENSATION_DOT_BYTES = 1_000_000
_GRAPH_NAME = "graphrag_condensation"
# Schema-1 producer caps. Kept local so this serializer stays independent of
# graph loading; regression tests exercise the matching byog_graph limits.
_HARD_MAX_COMPONENTS = 100
_HARD_MAX_NODES_PER_COMPONENT = 100
_HARD_MAX_EDGES = 500

_REQUIRED_TOP = (
    "edge_types",
    "max_components",
    "max_nodes_per_component",
    "max_edges",
    "components",
    "edges",
    "n_components_total",
    "n_components_returned",
    "n_nodes_total",
    "n_edges_total",
    "n_internal_edges_total",
    "n_cross_component_edges_total",
    "n_self_loop_edges_total",
    "n_cyclic_components_total",
    "n_entity_nodes_total",
    "n_endpoint_only_nodes_total",
    "n_condensation_edges_total",
    "n_condensation_edges_eligible_total",
    "n_condensation_edges_returned",
    "components_truncated",
    "nodes_truncated",
    "edges_truncated",
)
_REQUIRED_COMPONENT = (
    "representative",
    "nodes",
    "n_nodes_total",
    "n_nodes_returned",
    "n_internal_edges_total",
    "n_self_loop_edges_total",
    "n_entity_nodes",
    "n_endpoint_only_nodes",
    "is_cyclic",
    "nodes_truncated",
)
_REQUIRED_EDGE = ("source", "target", "n_relationship_rows_total")


class CondensationDotError(ValueError):
    """Invalid condensation DOT input, unencodable value, or byte-limit overflow."""


def dumps_condensation_dot(result: Mapping[str, Any]) -> str:
    """Serialize a condensation producer result to deterministic DOT.

    The returned string is the complete payload, including the final
    newline, and is guaranteed to be at most
    :data:`HARD_MAX_CONDENSATION_DOT_BYTES` UTF-8 bytes.
    """
    try:
        return _dumps_condensation_dot(result)
    except SubgraphDotError as exc:
        raise CondensationDotError(str(exc)) from exc


def _dumps_condensation_dot(result: Mapping[str, Any]) -> str:
    if not isinstance(result, Mapping):
        raise CondensationDotError("condensation DOT input must be a mapping")
    for name in _REQUIRED_TOP:
        if name not in result:
            raise CondensationDotError(f"missing condensation field {name}")

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
    max_edges = _require_int(
        result.get("max_edges"),
        "max_edges",
        minimum=0,
        maximum=_HARD_MAX_EDGES,
    )
    n_components_total = _require_int(
        result.get("n_components_total"), "n_components_total"
    )
    n_components_returned = _require_int(
        result.get("n_components_returned"), "n_components_returned"
    )
    n_nodes_total = _require_int(result.get("n_nodes_total"), "n_nodes_total")
    n_edges_total = _require_int(result.get("n_edges_total"), "n_edges_total")
    n_internal_edges_total = _require_int(
        result.get("n_internal_edges_total"), "n_internal_edges_total"
    )
    n_cross_component_edges_total = _require_int(
        result.get("n_cross_component_edges_total"),
        "n_cross_component_edges_total",
    )
    n_self_loop_edges_total = _require_int(
        result.get("n_self_loop_edges_total"), "n_self_loop_edges_total"
    )
    n_cyclic_components_total = _require_int(
        result.get("n_cyclic_components_total"), "n_cyclic_components_total"
    )
    n_entity_nodes_total = _require_int(
        result.get("n_entity_nodes_total"), "n_entity_nodes_total"
    )
    n_endpoint_only_nodes_total = _require_int(
        result.get("n_endpoint_only_nodes_total"),
        "n_endpoint_only_nodes_total",
    )
    n_condensation_edges_total = _require_int(
        result.get("n_condensation_edges_total"), "n_condensation_edges_total"
    )
    n_condensation_edges_eligible_total = _require_int(
        result.get("n_condensation_edges_eligible_total"),
        "n_condensation_edges_eligible_total",
    )
    n_condensation_edges_returned = _require_int(
        result.get("n_condensation_edges_returned"),
        "n_condensation_edges_returned",
    )
    components_truncated = _require_bool(
        result.get("components_truncated"), "components_truncated"
    )
    nodes_truncated = _require_bool(
        result.get("nodes_truncated"), "nodes_truncated"
    )
    edges_truncated = _require_bool(
        result.get("edges_truncated"), "edges_truncated"
    )
    components = _require_list(result.get("components"), "components")
    edges = _require_list(result.get("edges"), "edges")
    edge_types_token = _edge_types_token(result.get("edge_types"))

    if n_components_returned != len(components):
        raise CondensationDotError("n_components_returned does not match components")
    if n_condensation_edges_returned != len(edges):
        raise CondensationDotError(
            "n_condensation_edges_returned does not match edges"
        )
    if n_components_returned > n_components_total:
        raise CondensationDotError("returned component count exceeds total")
    if n_components_returned > max_components:
        raise CondensationDotError("returned component count exceeds max_components")
    if n_condensation_edges_returned > n_condensation_edges_eligible_total:
        raise CondensationDotError(
            "returned condensation-edge count exceeds eligible total"
        )
    if n_condensation_edges_eligible_total > n_condensation_edges_total:
        raise CondensationDotError(
            "eligible condensation-edge count exceeds total"
        )
    if n_condensation_edges_returned > max_edges:
        raise CondensationDotError(
            "returned condensation-edge count exceeds max_edges"
        )
    if components_truncated != (n_components_returned < n_components_total):
        raise CondensationDotError(
            "components_truncated disagrees with component counts"
        )
    if components_truncated and n_components_returned != max_components:
        raise CondensationDotError(
            "truncated components must return exactly max_components"
        )
    if (not components_truncated) and n_components_returned != n_components_total:
        raise CondensationDotError(
            "untruncated components must return every component"
        )
    if edges_truncated != (
        n_condensation_edges_returned < n_condensation_edges_total
    ):
        raise CondensationDotError(
            "edges_truncated disagrees with condensation-edge counts"
        )
    if (
        n_condensation_edges_returned < n_condensation_edges_eligible_total
        and n_condensation_edges_returned != max_edges
    ):
        raise CondensationDotError(
            "eligible condensation edges were truncated without max_edges"
        )
    if n_internal_edges_total + n_cross_component_edges_total != n_edges_total:
        raise CondensationDotError(
            "internal and cross-component edges do not cover selected rows"
        )
    if n_condensation_edges_total > n_cross_component_edges_total:
        raise CondensationDotError(
            "condensation-edge total exceeds cross-component relationship rows"
        )
    max_condensation_edges = n_components_total * (n_components_total - 1) // 2
    if n_condensation_edges_total > max_condensation_edges:
        raise CondensationDotError(
            "condensation-edge total exceeds the DAG component-pair bound"
        )
    max_eligible_edges = (
        n_components_returned * (n_components_returned - 1) // 2
    )
    if n_condensation_edges_eligible_total > max_eligible_edges:
        raise CondensationDotError(
            "eligible condensation-edge total exceeds returned component pairs"
        )
    if n_self_loop_edges_total > n_internal_edges_total:
        raise CondensationDotError("self-loop total exceeds internal-edge total")
    if n_entity_nodes_total + n_endpoint_only_nodes_total != n_nodes_total:
        raise CondensationDotError(
            "entity and endpoint-only counts do not cover the node universe"
        )
    if n_cyclic_components_total > n_components_total:
        raise CondensationDotError(
            "cyclic component count exceeds component total"
        )
    if n_components_total > 0 and n_nodes_total < n_components_total:
        raise CondensationDotError("node total is smaller than component total")
    if not components_truncated:
        if n_condensation_edges_eligible_total != n_condensation_edges_total:
            raise CondensationDotError(
                "untruncated components must make every condensation edge eligible"
            )
    if n_components_total == 0:
        _require_empty_result(
            n_components_returned=n_components_returned,
            n_nodes_total=n_nodes_total,
            n_edges_total=n_edges_total,
            n_internal_edges_total=n_internal_edges_total,
            n_cross_component_edges_total=n_cross_component_edges_total,
            n_self_loop_edges_total=n_self_loop_edges_total,
            n_cyclic_components_total=n_cyclic_components_total,
            n_entity_nodes_total=n_entity_nodes_total,
            n_endpoint_only_nodes_total=n_endpoint_only_nodes_total,
            n_condensation_edges_total=n_condensation_edges_total,
            n_condensation_edges_eligible_total=n_condensation_edges_eligible_total,
            n_condensation_edges_returned=n_condensation_edges_returned,
            components_truncated=components_truncated,
            nodes_truncated=nodes_truncated,
            edges_truncated=edges_truncated,
            components=components,
            edges=edges,
        )

    graph_attrs: List[Tuple[str, str]] = [
        ("schema_version", str(CONDENSATION_DOT_SCHEMA_VERSION)),
        ("max_components", str(max_components)),
        ("max_nodes_per_component", str(max_nodes_per_component)),
        ("max_edges", str(max_edges)),
        ("edge_types", edge_types_token),
        ("n_components_total", str(n_components_total)),
        ("n_components_returned", str(n_components_returned)),
        ("n_nodes_total", str(n_nodes_total)),
        ("n_edges_total", str(n_edges_total)),
        ("n_internal_edges_total", str(n_internal_edges_total)),
        ("n_cross_component_edges_total", str(n_cross_component_edges_total)),
        ("n_self_loop_edges_total", str(n_self_loop_edges_total)),
        ("n_cyclic_components_total", str(n_cyclic_components_total)),
        ("n_entity_nodes_total", str(n_entity_nodes_total)),
        ("n_endpoint_only_nodes_total", str(n_endpoint_only_nodes_total)),
        ("n_condensation_edges_total", str(n_condensation_edges_total)),
        (
            "n_condensation_edges_eligible_total",
            str(n_condensation_edges_eligible_total),
        ),
        ("n_condensation_edges_returned", str(n_condensation_edges_returned)),
        ("components_truncated", _dot_bool(components_truncated)),
        ("nodes_truncated", _dot_bool(nodes_truncated)),
        ("edges_truncated", _dot_bool(edges_truncated)),
    ]

    representative_to_id: Dict[str, str] = {}
    representative_index: Dict[str, int] = {}
    seen_titles: set[str] = set()
    component_lines: List[str] = []
    returned_node_titles = 0
    returned_internal = 0
    returned_self_loops = 0
    returned_entity = 0
    returned_endpoint_only = 0
    returned_cyclic = 0
    returned_nodes_total = 0
    any_component_nodes_truncated = False

    for index, component in enumerate(components):
        if not isinstance(component, Mapping):
            raise CondensationDotError("condensation component must be a mapping")
        for name in _REQUIRED_COMPONENT:
            if name not in component:
                raise CondensationDotError(f"missing component field {name}")
        representative = _require_nonempty_str(
            component.get("representative"), "component representative"
        )
        if representative in representative_to_id:
            raise CondensationDotError(
                f"duplicate condensation representative {representative!r}"
            )
        nodes = _require_str_list(component.get("nodes"), "component nodes")
        n_comp_nodes_total = _require_int(
            component.get("n_nodes_total"), "component n_nodes_total", minimum=1
        )
        n_comp_nodes_returned = _require_int(
            component.get("n_nodes_returned"),
            "component n_nodes_returned",
            minimum=1,
        )
        n_internal = _require_int(
            component.get("n_internal_edges_total"),
            "component n_internal_edges_total",
        )
        n_self = _require_int(
            component.get("n_self_loop_edges_total"),
            "component n_self_loop_edges_total",
        )
        n_entity = _require_int(
            component.get("n_entity_nodes"), "component n_entity_nodes"
        )
        n_endpoint_only = _require_int(
            component.get("n_endpoint_only_nodes"),
            "component n_endpoint_only_nodes",
        )
        is_cyclic = _require_bool(component.get("is_cyclic"), "component is_cyclic")
        comp_nodes_truncated = _require_bool(
            component.get("nodes_truncated"), "component nodes_truncated"
        )
        if n_comp_nodes_returned != len(nodes):
            raise CondensationDotError(
                "n_nodes_returned does not match component nodes"
            )
        if n_comp_nodes_returned > n_comp_nodes_total:
            raise CondensationDotError(
                "returned component node count exceeds component total"
            )
        if n_comp_nodes_returned > max_nodes_per_component:
            raise CondensationDotError(
                "returned component node count exceeds max_nodes_per_component"
            )
        if comp_nodes_truncated != (n_comp_nodes_returned < n_comp_nodes_total):
            raise CondensationDotError(
                "component nodes_truncated disagrees with node counts"
            )
        if comp_nodes_truncated and n_comp_nodes_returned != max_nodes_per_component:
            raise CondensationDotError(
                "truncated component nodes must return exactly max_nodes_per_component"
            )
        if (not comp_nodes_truncated) and n_comp_nodes_returned != n_comp_nodes_total:
            raise CondensationDotError(
                "untruncated component must return every member"
            )
        if n_entity + n_endpoint_only != n_comp_nodes_total:
            raise CondensationDotError(
                "component entity and endpoint-only counts do not cover members"
            )
        if n_self > n_internal:
            raise CondensationDotError(
                "component self-loop total exceeds internal-edge total"
            )
        expected_cyclic = n_comp_nodes_total > 1 or n_self > 0
        if is_cyclic != expected_cyclic:
            raise CondensationDotError(
                "component is_cyclic disagrees with size and self-loop totals"
            )
        if nodes != sorted(nodes, key=lambda title: title.encode("utf-8")):
            raise CondensationDotError(
                "component nodes must use canonical UTF-8 byte order"
            )
        if nodes[0] != representative:
            raise CondensationDotError(
                "component representative must be the first returned node"
            )
        for title in nodes:
            if title in seen_titles:
                raise CondensationDotError(
                    f"duplicate condensation node title {title!r}"
                )
            seen_titles.add(title)
        ident = f"c{index:04d}"
        representative_to_id[representative] = ident
        representative_index[representative] = index
        state = "cyclic" if is_cyclic else "acyclic"
        label = (
            f"{representative} ({n_comp_nodes_returned}/{n_comp_nodes_total}, {state})"
        )
        attrs: List[Tuple[str, str]] = [
            ("label", label),
            ("representative", representative),
            ("nodes", _json_array(nodes)),
            ("n_nodes_total", str(n_comp_nodes_total)),
            ("n_nodes_returned", str(n_comp_nodes_returned)),
            ("n_internal_edges_total", str(n_internal)),
            ("n_self_loop_edges_total", str(n_self)),
            ("n_entity_nodes", str(n_entity)),
            ("n_endpoint_only_nodes", str(n_endpoint_only)),
            ("is_cyclic", _dot_bool(is_cyclic)),
            ("nodes_truncated", _dot_bool(comp_nodes_truncated)),
        ]
        component_lines.append(f"  {ident} [{_format_attrs(attrs)}];")
        returned_node_titles += n_comp_nodes_returned
        returned_internal += n_internal
        returned_self_loops += n_self
        returned_entity += n_entity
        returned_endpoint_only += n_endpoint_only
        returned_nodes_total += n_comp_nodes_total
        if is_cyclic:
            returned_cyclic += 1
        if comp_nodes_truncated:
            any_component_nodes_truncated = True

    if nodes_truncated != any_component_nodes_truncated:
        raise CondensationDotError(
            "nodes_truncated disagrees with returned component flags"
        )
    if returned_cyclic > n_cyclic_components_total:
        raise CondensationDotError(
            "returned cyclic components exceed n_cyclic_components_total"
        )
    if returned_internal > n_internal_edges_total:
        raise CondensationDotError(
            "returned internal edges exceed n_internal_edges_total"
        )
    if returned_self_loops > n_self_loop_edges_total:
        raise CondensationDotError(
            "returned self-loop edges exceed n_self_loop_edges_total"
        )
    if returned_entity > n_entity_nodes_total:
        raise CondensationDotError(
            "returned entity nodes exceed n_entity_nodes_total"
        )
    if returned_endpoint_only > n_endpoint_only_nodes_total:
        raise CondensationDotError(
            "returned endpoint-only nodes exceed n_endpoint_only_nodes_total"
        )
    if returned_nodes_total > n_nodes_total:
        raise CondensationDotError(
            "returned component node totals exceed n_nodes_total"
        )
    omitted_components = n_components_total - n_components_returned
    if n_nodes_total - returned_nodes_total < omitted_components:
        raise CondensationDotError(
            "node total cannot provide one member for each omitted component"
        )
    if not components_truncated:
        if returned_nodes_total != n_nodes_total:
            raise CondensationDotError(
                "untruncated components must cover n_nodes_total"
            )
        if returned_internal != n_internal_edges_total:
            raise CondensationDotError(
                "untruncated components must cover n_internal_edges_total"
            )
        if returned_self_loops != n_self_loop_edges_total:
            raise CondensationDotError(
                "untruncated components must cover n_self_loop_edges_total"
            )
        if returned_entity != n_entity_nodes_total:
            raise CondensationDotError(
                "untruncated components must cover n_entity_nodes_total"
            )
        if returned_endpoint_only != n_endpoint_only_nodes_total:
            raise CondensationDotError(
                "untruncated components must cover n_endpoint_only_nodes_total"
            )
        if returned_cyclic != n_cyclic_components_total:
            raise CondensationDotError(
                "untruncated components must cover n_cyclic_components_total"
            )

    edge_lines: List[str] = []
    seen_pairs: set[Tuple[str, str]] = set()
    previous_positions: Tuple[int, int] | None = None
    returned_row_count = 0
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise CondensationDotError("condensation edge must be a mapping")
        for name in _REQUIRED_EDGE:
            if name not in edge:
                raise CondensationDotError(f"missing edge field {name}")
        source = _require_nonempty_str(edge.get("source"), "edge source")
        target = _require_nonempty_str(edge.get("target"), "edge target")
        rows = _require_int(
            edge.get("n_relationship_rows_total"),
            "edge n_relationship_rows_total",
            minimum=1,
        )
        pair = (source, target)
        if pair in seen_pairs:
            raise CondensationDotError(
                f"duplicate condensation edge {source!r} -> {target!r}"
            )
        seen_pairs.add(pair)
        if source == target:
            raise CondensationDotError("condensation edge endpoints must differ")
        src_id = representative_to_id.get(source)
        tgt_id = representative_to_id.get(target)
        if src_id is None or tgt_id is None:
            raise CondensationDotError(
                "condensation edge endpoint is not a returned component"
            )
        if representative_index[source] >= representative_index[target]:
            raise CondensationDotError(
                "condensation edge is not forward in producer component order"
            )
        positions = (representative_index[source], representative_index[target])
        if previous_positions is not None and positions <= previous_positions:
            raise CondensationDotError(
                "condensation edges are not in canonical producer order"
            )
        previous_positions = positions
        attrs = [
            ("label", f"rows {rows}"),
            ("source", source),
            ("target", target),
            ("n_relationship_rows_total", str(rows)),
        ]
        edge_lines.append(f"  {src_id} -> {tgt_id} [{_format_attrs(attrs)}];")
        returned_row_count += rows

    if returned_row_count > n_cross_component_edges_total:
        raise CondensationDotError(
            "returned condensation-edge row counts exceed cross-component total"
        )
    if (not edges_truncated) and (not components_truncated):
        if returned_row_count != n_cross_component_edges_total:
            raise CondensationDotError(
                "untruncated condensation edges must cover cross-component rows"
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
    lines.extend(component_lines)
    lines.extend(edge_lines)
    lines.append("}")
    payload = "\n".join(lines) + "\n"
    return _checked_payload(payload)


def _require_empty_result(
    *,
    n_components_returned: int,
    n_nodes_total: int,
    n_edges_total: int,
    n_internal_edges_total: int,
    n_cross_component_edges_total: int,
    n_self_loop_edges_total: int,
    n_cyclic_components_total: int,
    n_entity_nodes_total: int,
    n_endpoint_only_nodes_total: int,
    n_condensation_edges_total: int,
    n_condensation_edges_eligible_total: int,
    n_condensation_edges_returned: int,
    components_truncated: bool,
    nodes_truncated: bool,
    edges_truncated: bool,
    components: List[Any],
    edges: List[Any],
) -> None:
    if components or edges:
        raise CondensationDotError("empty condensation must have empty material")
    zeros = (
        n_components_returned,
        n_nodes_total,
        n_edges_total,
        n_internal_edges_total,
        n_cross_component_edges_total,
        n_self_loop_edges_total,
        n_cyclic_components_total,
        n_entity_nodes_total,
        n_endpoint_only_nodes_total,
        n_condensation_edges_total,
        n_condensation_edges_eligible_total,
        n_condensation_edges_returned,
    )
    if any(value != 0 for value in zeros):
        raise CondensationDotError("empty condensation must have zero totals")
    if components_truncated or nodes_truncated or edges_truncated:
        raise CondensationDotError("empty condensation must not be truncated")


def _checked_payload(payload: str) -> str:
    if not isinstance(payload, str):
        raise CondensationDotError("DOT payload must be str")
    if not payload.endswith("\n") or payload.endswith("\n\n"):
        raise CondensationDotError("DOT payload must end with exactly one newline")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CondensationDotError("condensation DOT is not strict UTF-8") from exc
    if b"\r" in encoded:
        raise CondensationDotError("DOT payload must not contain raw carriage returns")
    if any(byte < 0x20 and byte != 0x0A or byte == 0x7F for byte in encoded):
        raise CondensationDotError("DOT payload contains a raw control character")
    if len(encoded) > HARD_MAX_CONDENSATION_DOT_BYTES:
        raise CondensationDotError(
            f"condensation DOT exceeds hard limit of {HARD_MAX_CONDENSATION_DOT_BYTES} bytes"
        )
    return payload


def _format_attrs(attrs: Sequence[Tuple[str, str]]) -> str:
    return ", ".join(_attr(name, value) for name, value in attrs)


def _attr(name: str, value: str) -> str:
    if not name.isidentifier() or not name.isascii():
        raise CondensationDotError(f"invalid DOT attribute name {name!r}")
    return f"{name}={_quote(value)}"


def _quote(value: str) -> str:
    try:
        return quote_dot_string(value)
    except SubgraphDotError as exc:
        raise CondensationDotError(str(exc)) from exc


def _edge_types_token(raw: Any) -> str:
    if raw is None:
        return "null"
    if not isinstance(raw, list):
        raise CondensationDotError("edge_types must be a list or null")
    if not raw:
        raise CondensationDotError("edge_types must be non-empty when present")
    tokens: List[str] = []
    seen: set[str] = set()
    for item in raw:
        token = _require_nonempty_str(item, "edge type")
        if token.strip() != token or "\x00" in token:
            raise CondensationDotError(f"invalid edge_types value {token!r}")
        if token in seen:
            raise CondensationDotError(f"duplicate edge_types value {token!r}")
        seen.add(token)
        tokens.append(token)
    if tokens != sorted(tokens, key=lambda value: value.encode("utf-8")):
        raise CondensationDotError("edge_types must use canonical UTF-8 byte order")
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
        raise CondensationDotError("canonical JSON token is invalid") from exc


def _require_list(raw: Any, name: str) -> List[Any]:
    if not isinstance(raw, list):
        raise CondensationDotError(f"{name} must be a list")
    return raw


def _require_str_list(raw: Any, name: str) -> List[str]:
    items = _require_list(raw, name)
    titles: List[str] = []
    seen: set[str] = set()
    for item in items:
        title = _require_nonempty_str(item, name)
        if title in seen:
            raise CondensationDotError(f"duplicate {name} value {title!r}")
        seen.add(title)
        titles.append(title)
    return titles


def _require_bool(raw: Any, name: str) -> bool:
    if not isinstance(raw, bool):
        raise CondensationDotError(f"{name} must be a boolean")
    return raw


def _require_int(
    raw: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise CondensationDotError(f"{name} must be an integer")
    if raw < minimum:
        raise CondensationDotError(f"{name} must be >= {minimum}")
    if maximum is not None and raw > maximum:
        raise CondensationDotError(f"{name} must be <= {maximum}")
    return raw


def _require_str(raw: Any, name: str) -> str:
    if not isinstance(raw, str):
        raise CondensationDotError(f"{name} must be a string")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CondensationDotError(f"{name} is not strict UTF-8") from exc
    return raw


def _require_nonempty_str(raw: Any, name: str) -> str:
    text = _require_str(raw, name)
    if not text:
        raise CondensationDotError(f"{name} must be a non-empty string")
    return text


def _dot_bool(value: bool) -> str:
    return "true" if value else "false"
