"""Deterministic Graphviz DOT serialization for a shortest-path result.

This module renders an already-computed ``ByogGraph.shortest_path`` /
``compute_shortest_path`` mapping. It does not open a graph, resolve
endpoints, traverse or reconstruct relationships, invoke Graphviz, spawn
subprocesses, touch the network, or write temporary files. It does not
sort, expand, truncate, or otherwise modify producer material.

DOT here is a visualization **interchange** format on stdout. The project
does not render an image and does not provide an interactive UI.

Contract
========

* Directed, non-strict ``digraph graphrag_shortest_path``.
* Internal node identifiers are ``n0000``, ``n0001``, … in **producer
  node order**. Stored titles are never identifiers.
* Node and edge statements preserve producer order. This renderer does
  not sort or truncate.
* Every returned edge names the internal identifiers of consecutive path
  nodes. A missing or duplicate path title (except the zero-hop case) is
  a renderer error, not an omitted statement.
* Edges keep stored source-to-target orientation. Parallel relationship
  rows stay aggregated exactly as ``n_relationship_rows_total``.
* Graph-level ``source``, ``target``, ``distance``, and ``edge_types``
  are canonical JSON text: ``null`` is JSON null; a string or array
  preserves exact item boundaries (including commas and a literal
  ``"all"`` relationship type).
* Dynamic strings are emitted only as quoted DOT strings through
  :func:`graphrag_code.subgraph_dot.quote_dot_string`. No stored value is
  interpolated into an identifier, raw attribute, or comment.
* Control characters never appear raw. Invalid strict-UTF-8 strings,
  including lone surrogates, fail closed.
* Presentation-only attributes ``rankdir=LR``, ``shape=box``, and
  endpoint ``peripheries=2`` are fixed. They do not imply importance,
  ownership, execution, causality, or semantic meaning.
* Hard limit: :data:`HARD_MAX_SHORTEST_PATH_DOT_BYTES` UTF-8 bytes for
  the complete payload, including the final newline. Overflow fails
  before any caller write. The successful payload ends with exactly one
  newline.

A found zero-hop path emits one node and no edges. Non-found and
unresolved statuses render as a valid empty digraph with graph-level
metadata. That is not an error and is not a global-unreachability claim.
"""

from __future__ import annotations

import json
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from .subgraph_dot import SubgraphDotError, quote_dot_string

SHORTEST_PATH_DOT_SCHEMA_VERSION = 1
HARD_MAX_SHORTEST_PATH_DOT_BYTES = 1_000_000
_GRAPH_NAME = "graphrag_shortest_path"
# Schema-1 producer cap. Kept local so this serializer stays independent of
# graph loading; regression tests exercise the matching byog_graph limit.
_HARD_MAX_SHORTEST_PATH_DEPTH = 32
_STATUSES = (
    "found",
    "unresolved_source",
    "unresolved_target",
    "unresolved_both",
    "not_found_within_max_depth",
)
_REQUIRED_TOP = (
    "source",
    "target",
    "source_resolved",
    "target_resolved",
    "status",
    "found",
    "edge_types",
    "max_depth",
    "distance",
    "nodes",
    "steps",
    "n_nodes_returned",
    "n_steps_returned",
    "n_relationship_rows_on_path_total",
)
_REQUIRED_STEP = ("source", "target", "n_relationship_rows_total")


class ShortestPathDotError(ValueError):
    """Invalid shortest-path DOT input, unencodable value, or byte-limit overflow."""


def dumps_shortest_path_dot(result: Mapping[str, Any]) -> str:
    """Serialize a shortest-path producer result to deterministic DOT.

    The returned string is the complete payload, including the final
    newline, and is guaranteed to be at most
    :data:`HARD_MAX_SHORTEST_PATH_DOT_BYTES` UTF-8 bytes.
    """
    try:
        return _dumps_shortest_path_dot(result)
    except SubgraphDotError as exc:
        raise ShortestPathDotError(str(exc)) from exc


def _dumps_shortest_path_dot(result: Mapping[str, Any]) -> str:
    if not isinstance(result, Mapping):
        raise ShortestPathDotError("shortest-path DOT input must be a mapping")
    for name in _REQUIRED_TOP:
        if name not in result:
            raise ShortestPathDotError(f"missing shortest-path field {name}")

    status = _require_nonempty_str(result.get("status"), "status")
    if status not in _STATUSES:
        raise ShortestPathDotError(f"invalid shortest-path status {status!r}")
    found = _require_bool(result.get("found"), "found")
    if found != (status == "found"):
        raise ShortestPathDotError("found flag disagrees with status")
    source_resolved = _require_bool(result.get("source_resolved"), "source_resolved")
    target_resolved = _require_bool(result.get("target_resolved"), "target_resolved")
    source = _optional_title(result.get("source"), "source")
    target = _optional_title(result.get("target"), "target")
    _require_resolution(status, source_resolved, target_resolved, source, target)
    max_depth = _require_int(
        result.get("max_depth"),
        "max_depth",
        maximum=_HARD_MAX_SHORTEST_PATH_DEPTH,
    )
    distance = _optional_int(result.get("distance"), "distance")
    nodes = _require_list(result.get("nodes"), "nodes")
    steps = _require_list(result.get("steps"), "steps")
    n_nodes_returned = _require_int(
        result.get("n_nodes_returned"), "n_nodes_returned"
    )
    n_steps_returned = _require_int(
        result.get("n_steps_returned"), "n_steps_returned"
    )
    n_rows_total = _require_int(
        result.get("n_relationship_rows_on_path_total"),
        "n_relationship_rows_on_path_total",
    )
    edge_types_token = _edge_types_token(result.get("edge_types"))
    if n_nodes_returned != len(nodes):
        raise ShortestPathDotError("n_nodes_returned does not match nodes")
    if n_steps_returned != len(steps):
        raise ShortestPathDotError("n_steps_returned does not match steps")

    node_lines: List[str] = []
    edge_lines: List[str] = []
    if found:
        titles, row_sum = _validate_found(
            source=source,
            target=target,
            distance=distance,
            nodes=nodes,
            steps=steps,
        )
        if n_rows_total != row_sum:
            raise ShortestPathDotError(
                "n_relationship_rows_on_path_total does not match step row counts"
            )
        for index, title in enumerate(titles):
            is_source = index == 0
            is_target = index == len(titles) - 1
            attrs: List[Tuple[str, str]] = [
                ("label", title),
                ("title", title),
                ("path_index", str(index)),
                ("is_source", _dot_bool(is_source)),
                ("is_target", _dot_bool(is_target)),
            ]
            if is_source or is_target:
                attrs.append(("peripheries", "2"))
            ident = f"n{index:04d}"
            node_lines.append(f"  {ident} [{_format_attrs(attrs)}];")
        for index, step in enumerate(steps):
            attrs = [
                ("label", f"rows {step['n_relationship_rows_total']}"),
                ("source", step["source"]),
                ("target", step["target"]),
                ("step_index", str(index)),
                (
                    "n_relationship_rows_total",
                    str(step["n_relationship_rows_total"]),
                ),
            ]
            src_id = f"n{index:04d}"
            tgt_id = f"n{index + 1:04d}"
            edge_lines.append(f"  {src_id} -> {tgt_id} [{_format_attrs(attrs)}];")
    else:
        if distance is not None:
            raise ShortestPathDotError("non-found shortest path must have null distance")
        if nodes or steps:
            raise ShortestPathDotError("non-found shortest path must have empty material")
        if n_nodes_returned != 0 or n_steps_returned != 0 or n_rows_total != 0:
            raise ShortestPathDotError(
                "non-found shortest path must have zero returned/path counts"
            )

    graph_attrs: List[Tuple[str, str]] = [
        ("schema_version", str(SHORTEST_PATH_DOT_SCHEMA_VERSION)),
        ("status", status),
        ("found", _dot_bool(found)),
        ("source", _json_token(source)),
        ("target", _json_token(target)),
        ("source_resolved", _dot_bool(source_resolved)),
        ("target_resolved", _dot_bool(target_resolved)),
        ("edge_types", edge_types_token),
        ("max_depth", str(max_depth)),
        ("distance", _json_token(distance)),
        ("n_nodes_returned", str(n_nodes_returned)),
        ("n_steps_returned", str(n_steps_returned)),
        ("n_relationship_rows_on_path_total", str(n_rows_total)),
    ]
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


def _require_resolution(
    status: str,
    source_resolved: bool,
    target_resolved: bool,
    source: Optional[str],
    target: Optional[str],
) -> None:
    if status == "found" or status == "not_found_within_max_depth":
        expected_source, expected_target = True, True
    elif status == "unresolved_source":
        expected_source, expected_target = False, True
    elif status == "unresolved_target":
        expected_source, expected_target = True, False
    else:
        expected_source, expected_target = False, False
    if source_resolved != expected_source or target_resolved != expected_target:
        raise ShortestPathDotError("resolution flags disagree with status")
    if source_resolved:
        if source is None:
            raise ShortestPathDotError("resolved source must be a non-empty string")
    elif source is not None:
        raise ShortestPathDotError("unresolved source must be null")
    if target_resolved:
        if target is None:
            raise ShortestPathDotError("resolved target must be a non-empty string")
    elif target is not None:
        raise ShortestPathDotError("unresolved target must be null")


def _validate_found(
    *,
    source: Optional[str],
    target: Optional[str],
    distance: Optional[int],
    nodes: List[Any],
    steps: List[Any],
) -> Tuple[List[str], int]:
    if source is None or target is None:
        raise ShortestPathDotError("found shortest path is missing an endpoint")
    if distance is None:
        raise ShortestPathDotError("found shortest-path distance must be an integer")
    if distance != len(steps):
        raise ShortestPathDotError("found shortest-path distance must equal len(steps)")
    titles: List[str] = []
    seen: set[str] = set()
    for item in nodes:
        title = _require_nonempty_str(item, "path node")
        if title in seen:
            raise ShortestPathDotError(f"duplicate shortest-path node title {title!r}")
        seen.add(title)
        titles.append(title)
    if len(titles) != distance + 1:
        raise ShortestPathDotError("found shortest path must have distance+1 nodes")
    if titles[0] != source or titles[-1] != target:
        raise ShortestPathDotError(
            "found shortest path must start at source and end at target"
        )
    if distance == 0 and source != target:
        raise ShortestPathDotError("zero-hop shortest path requires source equal target")
    row_sum = 0
    for index, step in enumerate(steps):
        if not isinstance(step, Mapping):
            raise ShortestPathDotError("shortest-path step must be a mapping")
        if set(step) != set(_REQUIRED_STEP):
            raise ShortestPathDotError("shortest-path step has extra or missing fields")
        step_source = _require_nonempty_str(step.get("source"), "step source")
        step_target = _require_nonempty_str(step.get("target"), "step target")
        rows = _require_int(
            step.get("n_relationship_rows_total"),
            "step n_relationship_rows_total",
            minimum=1,
        )
        if step_source != titles[index] or step_target != titles[index + 1]:
            raise ShortestPathDotError(
                "shortest-path step does not connect consecutive nodes"
            )
        row_sum += rows
    return titles, row_sum


def _checked_payload(payload: str) -> str:
    if not isinstance(payload, str):
        raise ShortestPathDotError("DOT payload must be str")
    if not payload.endswith("\n") or payload.endswith("\n\n"):
        raise ShortestPathDotError("DOT payload must end with exactly one newline")
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ShortestPathDotError("shortest-path DOT is not strict UTF-8") from exc
    if b"\r" in encoded:
        raise ShortestPathDotError("DOT payload must not contain raw carriage returns")
    if any(byte < 0x20 and byte != 0x0A or byte == 0x7F for byte in encoded):
        raise ShortestPathDotError("DOT payload contains a raw control character")
    if len(encoded) > HARD_MAX_SHORTEST_PATH_DOT_BYTES:
        raise ShortestPathDotError(
            f"shortest-path DOT exceeds hard limit of "
            f"{HARD_MAX_SHORTEST_PATH_DOT_BYTES} bytes"
        )
    return payload


def _format_attrs(attrs: Sequence[Tuple[str, str]]) -> str:
    return ", ".join(_attr(name, value) for name, value in attrs)


def _attr(name: str, value: str) -> str:
    if not name.isidentifier() or not name.isascii():
        raise ShortestPathDotError(f"invalid DOT attribute name {name!r}")
    return f"{name}={_quote(value)}"


def _quote(value: str) -> str:
    try:
        return quote_dot_string(value)
    except SubgraphDotError as exc:
        raise ShortestPathDotError(str(exc)) from exc


def _edge_types_token(raw: Any) -> str:
    if raw is None:
        return "null"
    if not isinstance(raw, list):
        raise ShortestPathDotError("edge_types must be a list or null")
    if not raw:
        raise ShortestPathDotError("edge_types must be non-empty when present")
    tokens: List[str] = []
    seen: set[str] = set()
    for item in raw:
        token = _require_nonempty_str(item, "edge type")
        if token.strip() != token or "\x00" in token:
            raise ShortestPathDotError(f"invalid edge_types value {token!r}")
        if token in seen:
            raise ShortestPathDotError(f"duplicate edge_types value {token!r}")
        seen.add(token)
        tokens.append(token)
    if tokens != sorted(tokens, key=lambda value: value.encode("utf-8")):
        raise ShortestPathDotError("edge_types must use canonical UTF-8 byte order")
    return _json_array(tokens)


def _json_array(values: Sequence[str]) -> str:
    return _json_token(list(values))


def _json_token(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ShortestPathDotError("canonical JSON token is invalid") from exc


def _require_list(raw: Any, name: str) -> List[Any]:
    if not isinstance(raw, list):
        raise ShortestPathDotError(f"{name} must be a list")
    return raw


def _require_bool(raw: Any, name: str) -> bool:
    if not isinstance(raw, bool):
        raise ShortestPathDotError(f"{name} must be a boolean")
    return raw


def _require_int(
    raw: Any,
    name: str,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ShortestPathDotError(f"{name} must be an integer")
    if raw < minimum:
        raise ShortestPathDotError(f"{name} must be >= {minimum}")
    if maximum is not None and raw > maximum:
        raise ShortestPathDotError(f"{name} must be <= {maximum}")
    return raw


def _optional_int(raw: Any, name: str) -> Optional[int]:
    if raw is None:
        return None
    return _require_int(raw, name)


def _require_str(raw: Any, name: str) -> str:
    if not isinstance(raw, str):
        raise ShortestPathDotError(f"{name} must be a string")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ShortestPathDotError(f"{name} is not strict UTF-8") from exc
    return raw


def _require_nonempty_str(raw: Any, name: str) -> str:
    text = _require_str(raw, name)
    if not text:
        raise ShortestPathDotError(f"{name} must be a non-empty string")
    return text


def _optional_title(raw: Any, name: str) -> Optional[str]:
    if raw is None:
        return None
    return _require_nonempty_str(raw, name)


def _dot_bool(value: bool) -> str:
    return "true" if value else "false"
