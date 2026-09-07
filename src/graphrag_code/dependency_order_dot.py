"""Deterministic Graphviz DOT serialization for a containment-order title list.

This module renders an already-computed ``ByogGraph.dependency_order`` /
``compute_containment_dependency_order`` ``List[str]``. It does not open a
graph, inspect a snapshot, reread entities or relationship rows,
reconstruct ``contains`` edges, recompute SCCs, invoke Graphviz, spawn
subprocesses, touch the network, or write temporary files. It does not
sort, expand, truncate, or otherwise modify producer material. The
producer returns only an ordered title list, so this renderer emits no
edge statements and does not invent, infer, or reconstruct edges, SCC
clusters, rank constraints, or a synthetic root.

DOT here is a visualization **interchange** format on stdout. The project
does not render an image and does not provide an interactive UI.
Graphviz is not invoked. Statement order follows the producer list;
that is not a claim that a rendered Graphviz layout will preserve that
order. Internal identifiers ``n0000``, ``n0001``, … are serialization
identifiers only: they are not an ordinal rank field, layer, depth, or
semantic score.

Contract
========

* Directed, non-strict ``digraph graphrag_dependency_order``.
* Graph metadata is only values derivable from the list:
  ``schema_version`` and ``n_nodes_total``.
* Each title is one node statement in **exact producer-list order**.
  Internal identifiers are ``n0000``, ``n0001``, … Raw titles are never
  identifiers.
* Node attributes, in this exact order: ``label``, ``title``.
* No relationship-edge, reconstructed ``contains``, invisible, or
  layout-only edge statements. No rank constraints, SCC clusters, or
  synthetic root.
* Dynamic strings are emitted only as quoted DOT strings through
  :func:`graphrag_code.subgraph_dot.quote_dot_string`. No stored value is
  interpolated into an identifier, raw attribute, or comment.
* Control characters never appear raw. Invalid strict-UTF-8 strings,
  including lone surrogates, fail closed.
* Presentation-only attributes ``rankdir=LR`` and ``shape=box`` are
  fixed. They do not imply importance, ownership, architecture,
  leadership, hierarchy, or semantic meaning.
* Hard limit: :data:`HARD_MAX_DEPENDENCY_ORDER_DOT_BYTES` UTF-8 bytes
  for the complete payload, including the final newline. Overflow fails
  before any caller write. The successful payload ends with exactly one
  newline.

An empty producer list renders as a valid empty digraph with graph-level
metadata and no nodes. That is not an error. This is an unbounded
full-list interchange: the serializer does not truncate the producer
contract.

This is structural containment-order interchange of titles only: not a
containment DAG drawing, not a build, import, call, execution, or
semantic dependency graph, and not hierarchy, architecture, ownership,
importance, a porting plan, GraphRAG, or natural-language analysis.
"""

from __future__ import annotations

from typing import Any, List, Sequence, Tuple

from .subgraph_dot import SubgraphDotError, quote_dot_string

DEPENDENCY_ORDER_DOT_SCHEMA_VERSION = 1
HARD_MAX_DEPENDENCY_ORDER_DOT_BYTES = 1_000_000
_GRAPH_NAME = "graphrag_dependency_order"


class DependencyOrderDotError(ValueError):
    """Invalid dependency-order DOT input, unencodable value, or overflow."""


def dumps_dependency_order_dot(result: List[str]) -> str:
    """Serialize a containment-order title list to deterministic DOT.

    The returned string is the complete payload, including the final
    newline, and is guaranteed to be at most
    :data:`HARD_MAX_DEPENDENCY_ORDER_DOT_BYTES` UTF-8 bytes.
    """
    try:
        return _dumps_dependency_order_dot(result)
    except SubgraphDotError as exc:
        raise DependencyOrderDotError(str(exc)) from exc


def _dumps_dependency_order_dot(result: Any) -> str:
    if not isinstance(result, list) or isinstance(result, (str, bytes, bytearray)):
        raise DependencyOrderDotError("dependency-order DOT input must be a list")

    titles: List[str] = []
    seen: set[str] = set()
    for item in result:
        title = _require_nonempty_str(item, "dependency-order title")
        if title in seen:
            raise DependencyOrderDotError(
                f"duplicate dependency-order title {title!r}"
            )
        seen.add(title)
        titles.append(title)

    graph_attrs: List[Tuple[str, str]] = [
        ("schema_version", str(DEPENDENCY_ORDER_DOT_SCHEMA_VERSION)),
        ("n_nodes_total", str(len(titles))),
    ]

    node_lines: List[str] = []
    for index, title in enumerate(titles):
        ident = f"n{index:04d}"
        node_attrs: List[Tuple[str, str]] = [
            ("label", title),
            ("title", title),
        ]
        node_lines.append(f"  {ident} [{_format_attrs(node_attrs)}];")

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


def _checked_payload(payload: str) -> str:
    if not isinstance(payload, str):
        raise DependencyOrderDotError("DOT payload must be str")
    if not payload.endswith("\n") or payload.endswith("\n\n"):
        raise DependencyOrderDotError(
            "DOT payload must end with exactly one newline"
        )
    try:
        encoded = payload.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DependencyOrderDotError(
            "dependency-order DOT is not strict UTF-8"
        ) from exc
    if b"\r" in encoded:
        raise DependencyOrderDotError(
            "DOT payload must not contain raw carriage returns"
        )
    if any(byte < 0x20 and byte != 0x0A or byte == 0x7F for byte in encoded):
        raise DependencyOrderDotError("DOT payload contains a raw control character")
    if len(encoded) > HARD_MAX_DEPENDENCY_ORDER_DOT_BYTES:
        raise DependencyOrderDotError(
            "dependency-order DOT exceeds hard limit of "
            f"{HARD_MAX_DEPENDENCY_ORDER_DOT_BYTES} bytes"
        )
    return payload


def _format_attrs(attrs: Sequence[Tuple[str, str]]) -> str:
    return ", ".join(_attr(name, value) for name, value in attrs)


def _attr(name: str, value: str) -> str:
    if not name.isidentifier() or not name.isascii():
        raise DependencyOrderDotError(f"invalid DOT attribute name {name!r}")
    return f"{name}={_quote(value)}"


def _quote(value: str) -> str:
    try:
        return quote_dot_string(value)
    except SubgraphDotError as exc:
        raise DependencyOrderDotError(str(exc)) from exc


def _require_str(raw: Any, name: str) -> str:
    if not isinstance(raw, str):
        raise DependencyOrderDotError(f"{name} must be a string")
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DependencyOrderDotError(f"{name} is not strict UTF-8") from exc
    return raw


def _require_nonempty_str(raw: Any, name: str) -> str:
    text = _require_str(raw, name)
    if not text:
        raise DependencyOrderDotError(f"{name} must be a non-empty string")
    return text
