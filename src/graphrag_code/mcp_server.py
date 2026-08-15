#!/usr/bin/env python
"""Read-only MCP stdio server for one existing BYOG graph.

Local agents and editors can query a single graph selected at startup.
This is not an indexer, publisher, port-eval surface, HTTP service, or
semantic-search backend. stdout is MCP protocol traffic only.

Each tool call takes a shared ``.publish.lock`` reader lease, resolves
``current`` once while the lease is held, pins that snapshot for the rest
of the call, and reports the snapshot id. Cooperating publishers wait.
Manual deletion or corruption still returns a controlled error.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from pydantic import ConfigDict

from graphrag_code.byog_graph import (
    DEFAULT_TYPE_CLOSURE_MAX_DEPTH,
    DEFAULT_TYPE_CLOSURE_MAX_EDGES,
    DEFAULT_TYPE_CLOSURE_MAX_NODES,
    TYPE_CLOSURE_DIRECTIONS,
    ByogGraph,
    ByogReaderLockError,
    graph_read_lease,
)
from graphrag_code.byog_snapshot_graph_audit import (
    SnapshotGraphAuditError,
    resolve_snapshot,
)
from graphrag_code.context_pack import assemble_context_pack
from graphrag_code.persisted_graph_doctor import (
    PersistedGraphDoctorError,
    audit_graph_root,
    audit_to_json,
)
from graphrag_code.persisted_graph_integrity import AmbiguousIndexerError
from graphrag_code.snapshot_compare import (
    DEFAULT_DIFF_MAX_ITEMS,
    DEFAULT_HISTORY_LIMIT,
    HARD_MAX_DIFF_ITEMS,
    HARD_MAX_HISTORY_LIMIT,
    SnapshotCompareError,
    snapshot_diff as compare_snapshots,
    snapshot_history as list_snapshot_history,
)

SCHEMA_VERSION = 1
DEFAULT_MAX_ITEMS = 100
HARD_MAX_ITEMS = 500
HARD_MAX_DEPTH = 32
HARD_MAX_CLOSURE_NODES = 500
HARD_MAX_CLOSURE_EDGES = 500
HARD_MAX_TEXT_CHARS = 100_000
HARD_MAX_TYPE_DEPTH = 16
HARD_MAX_TYPE_EDGES = 500
HARD_MAX_TYPE_OBSERVATIONS = 100
HARD_MAX_ANOMALY_SAMPLES = 500
HARD_MAX_ENVELOPE_BYTES = 1_000_000
DEFAULT_MAX_ANOMALY_SAMPLES = 40
READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
TOOL_NAMES = (
    "graph_status",
    "graph_doctor",
    "query_symbol",
    "callers",
    "callees",
    "neighbors",
    "impact",
    "type_closure",
    "context_pack",
    "snapshot_history",
    "snapshot_diff",
)


class GraphMcpError(ValueError):
    """Expected data or validation failure for an MCP tool call."""


def _json_ready(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, default=str, allow_nan=False))


def _require_int(
    name: str,
    value: Any,
    *,
    minimum: int = 0,
    maximum: Optional[int] = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GraphMcpError(f"{name} must be an integer, got {value!r}")
    if value < minimum:
        raise GraphMcpError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise GraphMcpError(f"{name} must be <= {maximum}, got {value}")
    return value


def _require_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise GraphMcpError(f"{name} must be a boolean, got {value!r}")
    return value


def _require_str(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise GraphMcpError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _reject_non_finite(name: str, value: Any) -> None:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        raise GraphMcpError(f"{name} must be a finite number, got {value!r}")


def _truncate_list(items: Sequence[Any], max_items: int) -> Tuple[List[Any], int, int, bool]:
    values = list(items)
    total = len(values)
    if total > max_items:
        return values[:max_items], total, max_items, True
    return values, total, total, False


def _envelope(
    *,
    tool: str,
    graph: Path,
    snapshot: str,
    data: Any,
    limits: Optional[Dict[str, Any]] = None,
    truncated: bool = False,
    total: Optional[int] = None,
    returned: Optional[int] = None,
) -> Dict[str, Any]:
    normalized_limits = dict(limits or {})
    normalized_limits["max_envelope_bytes"] = HARD_MAX_ENVELOPE_BYTES
    body: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": tool,
        "graph": str(graph),
        "snapshot": snapshot,
        "ok": True,
        "data": _json_ready(data),
        "limits": normalized_limits,
        "truncated": bool(truncated),
    }
    if total is not None:
        body["total"] = total
    if returned is not None:
        body["returned"] = returned
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > HARD_MAX_ENVELOPE_BYTES:
        raise GraphMcpError(
            f"{tool} response envelope exceeds hard limit of "
            f"{HARD_MAX_ENVELOPE_BYTES} bytes; "
            "request a smaller result"
        )
    return body


def _pack_was_truncated(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if (
                isinstance(item, bool)
                and item
                and (key == "truncated" or key.endswith("_truncated"))
            ):
                return True
            if _pack_was_truncated(item):
                return True
    elif isinstance(value, list):
        return any(_pack_was_truncated(item) for item in value)
    return False


class GraphMcpSession:
    """One-graph read-only MCP session. Tools cannot change the graph root."""

    def __init__(
        self,
        graph_root: Path,
        *,
        configured_indexer: str,
        resolved_indexer: str,
        preflight: Mapping[str, Any],
    ) -> None:
        self.graph_root = Path(graph_root)
        self.configured_indexer = configured_indexer
        self.resolved_indexer = resolved_indexer
        self.preflight = dict(preflight)

    def pin(self) -> Tuple[Path, str, Dict[str, Any]]:
        try:
            snap_dir, snap_id, manifest = resolve_snapshot(self.graph_root, None)
        except SnapshotGraphAuditError as exc:
            raise GraphMcpError(str(exc)) from exc
        except OSError as exc:
            raise GraphMcpError(
                f"cannot resolve published snapshot under {self.graph_root}: {exc}"
            ) from exc
        if snap_id is None or not isinstance(manifest, Mapping):
            raise GraphMcpError(
                f"graph has no published current snapshot: {self.graph_root}"
            )
        return Path(snap_dir), str(snap_id), dict(manifest)

    def _graph(self, snap_dir: Path) -> ByogGraph:
        try:
            return ByogGraph(snap_dir)
        except FileNotFoundError as exc:
            raise GraphMcpError(
                f"pinned snapshot is no longer readable: {exc}"
            ) from exc
        except OSError as exc:
            raise GraphMcpError(
                f"pinned snapshot is no longer readable: {exc}"
            ) from exc

    def graph_status(self) -> Dict[str, Any]:
        try:
            with graph_read_lease(self.graph_root):
                _snap_dir, snap_id, manifest = self.pin()
                index_input = manifest.get("index_input")
                reuse_supported = None
                index_input_present = isinstance(index_input, Mapping)
                if index_input_present:
                    reuse_supported = bool(index_input.get("reuse_supported"))
                data = {
                    "graph": str(self.graph_root),
                    "snapshot": snap_id,
                    "indexer": self.resolved_indexer,
                    "indexer_configured": self.configured_indexer,
                    "indexer_resolution": dict(self.preflight.get("indexer_resolution") or {}),
                    "schema_version": manifest.get("schema_version"),
                    "counts": manifest.get("counts"),
                    "files": manifest.get("files"),
                    "index_input_present": index_input_present,
                    "reuse_supported": reuse_supported,
                }
                return _envelope(
                    tool="graph_status",
                    graph=self.graph_root,
                    snapshot=snap_id,
                    data=data,
                )
        except ByogReaderLockError as exc:
            raise GraphMcpError(str(exc)) from exc

    def graph_doctor(self, max_anomaly_samples: Any = DEFAULT_MAX_ANOMALY_SAMPLES) -> Dict[str, Any]:
        _reject_non_finite("max_anomaly_samples", max_anomaly_samples)
        samples = _require_int(
            "max_anomaly_samples",
            max_anomaly_samples,
            minimum=0,
            maximum=HARD_MAX_ANOMALY_SAMPLES,
        )
        try:
            report = audit_graph_root(
                self.graph_root,
                indexer=self.resolved_indexer,
                max_anomaly_samples=samples,
                allow_unlocked_managed=False,
            )
            snap_id = str(report["snapshot"])
            data = json.loads(audit_to_json(report))
            return _envelope(
                tool="graph_doctor",
                graph=self.graph_root,
                snapshot=snap_id,
                data=data,
                limits={"max_anomaly_samples": samples},
            )
        except (
            PersistedGraphDoctorError,
            SnapshotGraphAuditError,
            AmbiguousIndexerError,
            OSError,
            TypeError,
            ValueError,
            KeyError,
        ) as exc:
            raise GraphMcpError(str(exc)) from exc

    def query_symbol(self, symbol: Any) -> Dict[str, Any]:
        title = _require_str("symbol", symbol)
        try:
            with graph_read_lease(self.graph_root):
                _snap_dir, snap_id, _manifest = self.pin()
                graph = self._graph(_snap_dir)
                return _envelope(
                    tool="query_symbol",
                    graph=self.graph_root,
                    snapshot=snap_id,
                    data=graph.symbol(title),
                )
        except ByogReaderLockError as exc:
            raise GraphMcpError(str(exc)) from exc

    def callers(self, symbol: Any, max_items: Any = DEFAULT_MAX_ITEMS) -> Dict[str, Any]:
        return self._symbol_list("callers", symbol, max_items, lambda g, title: g.callers(title))

    def callees(self, symbol: Any, max_items: Any = DEFAULT_MAX_ITEMS) -> Dict[str, Any]:
        return self._symbol_list("callees", symbol, max_items, lambda g, title: g.callees(title))

    def impact(self, symbol: Any, max_items: Any = DEFAULT_MAX_ITEMS) -> Dict[str, Any]:
        return self._symbol_list("impact", symbol, max_items, lambda g, title: g.impact(title))

    def _symbol_list(self, tool: str, symbol: Any, max_items: Any, fn) -> Dict[str, Any]:
        title = _require_str("symbol", symbol)
        _reject_non_finite("max_items", max_items)
        limit = _require_int("max_items", max_items, minimum=1, maximum=HARD_MAX_ITEMS)
        try:
            with graph_read_lease(self.graph_root):
                snap_dir, snap_id, _manifest = self.pin()
                items, total, returned, truncated = _truncate_list(
                    fn(self._graph(snap_dir), title), limit
                )
                return _envelope(
                    tool=tool,
                    graph=self.graph_root,
                    snapshot=snap_id,
                    data=items,
                    limits={"max_items": limit},
                    truncated=truncated,
                    total=total,
                    returned=returned,
                )
        except ByogReaderLockError as exc:
            raise GraphMcpError(str(exc)) from exc

    def neighbors(self, symbol: Any, max_items: Any = DEFAULT_MAX_ITEMS) -> Dict[str, Any]:
        title = _require_str("symbol", symbol)
        _reject_non_finite("max_items", max_items)
        limit = _require_int("max_items", max_items, minimum=1, maximum=HARD_MAX_ITEMS)
        try:
            with graph_read_lease(self.graph_root):
                snap_dir, snap_id, _manifest = self.pin()
                raw = self._graph(snap_dir).neighbors(title)
                incoming, in_total, in_ret, in_trunc = _truncate_list(raw.get("incoming") or [], limit)
                outgoing, out_total, out_ret, out_trunc = _truncate_list(raw.get("outgoing") or [], limit)
                truncated = in_trunc or out_trunc
                return _envelope(
                    tool="neighbors",
                    graph=self.graph_root,
                    snapshot=snap_id,
                    data={"incoming": incoming, "outgoing": outgoing},
                    limits={"max_items": limit},
                    truncated=truncated,
                    total=in_total + out_total,
                    returned=in_ret + out_ret,
                )
        except ByogReaderLockError as exc:
            raise GraphMcpError(str(exc)) from exc

    def type_closure(
        self,
        symbol: Any,
        direction: Any = "dependencies",
        max_depth: Any = DEFAULT_TYPE_CLOSURE_MAX_DEPTH,
        max_nodes: Any = DEFAULT_TYPE_CLOSURE_MAX_NODES,
        max_edges: Any = DEFAULT_TYPE_CLOSURE_MAX_EDGES,
    ) -> Dict[str, Any]:
        title = _require_str("symbol", symbol)
        if not isinstance(direction, str) or direction not in TYPE_CLOSURE_DIRECTIONS:
            raise GraphMcpError(
                f"direction must be one of {sorted(TYPE_CLOSURE_DIRECTIONS)}, got {direction!r}"
            )
        for name, value in (
            ("max_depth", max_depth),
            ("max_nodes", max_nodes),
            ("max_edges", max_edges),
        ):
            _reject_non_finite(name, value)
        depth = _require_int("max_depth", max_depth, minimum=0, maximum=HARD_MAX_DEPTH)
        nodes = _require_int("max_nodes", max_nodes, minimum=0, maximum=HARD_MAX_CLOSURE_NODES)
        edges = _require_int("max_edges", max_edges, minimum=0, maximum=HARD_MAX_CLOSURE_EDGES)
        try:
            with graph_read_lease(self.graph_root):
                snap_dir, snap_id, _manifest = self.pin()
                try:
                    result = self._graph(snap_dir).type_closure(
                        title,
                        direction=direction,
                        max_depth=depth,
                        max_nodes=nodes,
                        max_edges=edges,
                    )
                except ValueError as exc:
                    raise GraphMcpError(str(exc)) from exc
                truncated = bool(result.get("nodes_truncated") or result.get("edges_truncated"))
                return _envelope(
                    tool="type_closure",
                    graph=self.graph_root,
                    snapshot=snap_id,
                    data=result,
                    limits={
                        "max_depth": depth,
                        "max_nodes": nodes,
                        "max_edges": edges,
                    },
                    truncated=truncated,
                )
        except ByogReaderLockError as exc:
            raise GraphMcpError(str(exc)) from exc

    def context_pack(
        self,
        symbol: Any,
        purpose: Any = "port-to-rust",
        max_text_chars: Any = 300,
        neighbor_text: Any = True,
        max_type_edges: Any = 20,
        max_type_observations: Any = 5,
        type_depth: Any = 1,
    ) -> Dict[str, Any]:
        title = _require_str("symbol", symbol)
        purpose_s = _require_str("purpose", purpose)
        _reject_non_finite("max_text_chars", max_text_chars)
        _reject_non_finite("max_type_edges", max_type_edges)
        _reject_non_finite("max_type_observations", max_type_observations)
        _reject_non_finite("type_depth", type_depth)
        text_chars = _require_int(
            "max_text_chars", max_text_chars, minimum=1, maximum=HARD_MAX_TEXT_CHARS
        )
        neighbor = _require_bool("neighbor_text", neighbor_text)
        type_edges = _require_int(
            "max_type_edges", max_type_edges, minimum=0, maximum=HARD_MAX_TYPE_EDGES
        )
        type_obs = _require_int(
            "max_type_observations",
            max_type_observations,
            minimum=0,
            maximum=HARD_MAX_TYPE_OBSERVATIONS,
        )
        depth = _require_int("type_depth", type_depth, minimum=1, maximum=HARD_MAX_TYPE_DEPTH)
        try:
            with graph_read_lease(self.graph_root):
                snap_dir, snap_id, _manifest = self.pin()
                try:
                    pack = assemble_context_pack(
                        title,
                        snap_dir,
                        purpose=purpose_s,
                        max_text_chars=text_chars,
                        full_text=False,
                        neighbor_text=neighbor,
                        max_type_edges=type_edges,
                        max_type_observations=type_obs,
                        type_depth=depth,
                    )
                except FileNotFoundError as exc:
                    raise GraphMcpError(
                        f"pinned snapshot is no longer readable: {exc}"
                    ) from exc
                except ValueError as exc:
                    raise GraphMcpError(str(exc)) from exc
                truncated = _pack_was_truncated(pack)
                return _envelope(
                    tool="context_pack",
                    graph=self.graph_root,
                    snapshot=snap_id,
                    data=pack,
                    limits={
                        "max_text_chars": text_chars,
                        "max_type_edges": type_edges,
                        "max_type_observations": type_obs,
                        "type_depth": depth,
                        "neighbor_text": neighbor,
                    },
                    truncated=truncated,
                )
        except ByogReaderLockError as exc:
            raise GraphMcpError(str(exc)) from exc

    def snapshot_history(self, limit: Any = DEFAULT_HISTORY_LIMIT) -> Dict[str, Any]:
        _reject_non_finite("limit", limit)
        bound = _require_int(
            "limit", limit, minimum=1, maximum=HARD_MAX_HISTORY_LIMIT
        )
        try:
            result = list_snapshot_history(
                self.graph_root,
                limit=bound,
                allow_unlocked_legacy=False,
            )
            return _envelope(
                tool="snapshot_history",
                graph=self.graph_root,
                snapshot=str(result["current"]),
                data=result,
                limits={
                    "limit": bound,
                    "hard_max_limit": HARD_MAX_HISTORY_LIMIT,
                },
                truncated=_pack_was_truncated(result),
                total=int(result["total"]),
                returned=int(result["returned"]),
            )
        except ByogReaderLockError as exc:
            raise GraphMcpError(str(exc)) from exc
        except (
            SnapshotCompareError,
            SnapshotGraphAuditError,
            PersistedGraphDoctorError,
            OSError,
            TypeError,
            ValueError,
            KeyError,
        ) as exc:
            raise GraphMcpError(str(exc)) from exc

    def snapshot_diff(
        self,
        from_snapshot: Any,
        to_snapshot: Any = "current",
        max_items: Any = DEFAULT_DIFF_MAX_ITEMS,
    ) -> Dict[str, Any]:
        from_ref = _require_str("from_snapshot", from_snapshot)
        to_ref = _require_str("to_snapshot", to_snapshot)
        _reject_non_finite("max_items", max_items)
        bound = _require_int(
            "max_items", max_items, minimum=1, maximum=HARD_MAX_DIFF_ITEMS
        )
        try:
            result = compare_snapshots(
                self.graph_root,
                from_ref,
                to_ref,
                max_items=bound,
                allow_unlocked_legacy=False,
            )
            return _envelope(
                tool="snapshot_diff",
                graph=self.graph_root,
                snapshot=str(result["to_snapshot"]),
                data=result,
                limits={
                    "max_items": bound,
                    "hard_max_items": HARD_MAX_DIFF_ITEMS,
                },
                truncated=_pack_was_truncated(result),
                total=int((result.get("totals") or {}).get("added", 0))
                + int((result.get("totals") or {}).get("removed", 0))
                + int((result.get("totals") or {}).get("modified", 0)),
                returned=sum(
                    int((table.get(kind) or {}).get("returned", 0))
                    for table in (result.get("tables") or {}).values()
                    for kind in ("added", "removed", "modified")
                ),
            )
        except ByogReaderLockError as exc:
            raise GraphMcpError(str(exc)) from exc
        except (
            SnapshotCompareError,
            SnapshotGraphAuditError,
            PersistedGraphDoctorError,
            OSError,
            TypeError,
            ValueError,
            KeyError,
        ) as exc:
            raise GraphMcpError(str(exc)) from exc


def build_mcp_server(session: GraphMcpSession) -> MCPServer:
    mcp = MCPServer(
        "graphrag-code",
        version="0.1.0",
        instructions=(
            "Read-only BYOG graph tools for one graph selected at startup. "
            "No indexing, publishing, port-eval, or arbitrary filesystem access."
        ),
        log_level="WARNING",
    )

    @mcp.tool(
        name="graph_status",
        description="Read-only snapshot and indexer status.",
        annotations=READ_ONLY_TOOL,
    )
    def graph_status() -> Dict[str, Any]:
        return session.graph_status()

    @mcp.tool(
        name="graph_doctor",
        description="Read-only persisted-integrity doctor report.",
        annotations=READ_ONLY_TOOL,
    )
    def graph_doctor(
        max_anomaly_samples: int = DEFAULT_MAX_ANOMALY_SAMPLES,
    ) -> Dict[str, Any]:
        return session.graph_doctor(max_anomaly_samples)

    @mcp.tool(
        name="query_symbol",
        description="Look up one graph entity by title or unique partial.",
        annotations=READ_ONLY_TOOL,
    )
    def query_symbol(symbol: str) -> Dict[str, Any]:
        return session.query_symbol(symbol)

    @mcp.tool(
        name="callers",
        description="Direct callers of a symbol.",
        annotations=READ_ONLY_TOOL,
    )
    def callers(symbol: str, max_items: int = DEFAULT_MAX_ITEMS) -> Dict[str, Any]:
        return session.callers(symbol, max_items)

    @mcp.tool(
        name="callees",
        description="Direct callees of a symbol.",
        annotations=READ_ONLY_TOOL,
    )
    def callees(symbol: str, max_items: int = DEFAULT_MAX_ITEMS) -> Dict[str, Any]:
        return session.callees(symbol, max_items)

    @mcp.tool(
        name="neighbors",
        description="Incoming and outgoing neighbors of a symbol.",
        annotations=READ_ONLY_TOOL,
    )
    def neighbors(symbol: str, max_items: int = DEFAULT_MAX_ITEMS) -> Dict[str, Any]:
        return session.neighbors(symbol, max_items)

    @mcp.tool(
        name="impact",
        description="Cycle-safe transitive callers of a symbol.",
        annotations=READ_ONLY_TOOL,
    )
    def impact(symbol: str, max_items: int = DEFAULT_MAX_ITEMS) -> Dict[str, Any]:
        return session.impact(symbol, max_items)

    @mcp.tool(
        name="type_closure",
        description="Bounded cycle-safe transitive uses_type closure.",
        annotations=READ_ONLY_TOOL,
    )
    def type_closure(
        symbol: str,
        direction: str = "dependencies",
        max_depth: int = DEFAULT_TYPE_CLOSURE_MAX_DEPTH,
        max_nodes: int = DEFAULT_TYPE_CLOSURE_MAX_NODES,
        max_edges: int = DEFAULT_TYPE_CLOSURE_MAX_EDGES,
    ) -> Dict[str, Any]:
        return session.type_closure(symbol, direction, max_depth, max_nodes, max_edges)

    @mcp.tool(
        name="context_pack",
        description="Deterministic bounded structured context pack for a symbol.",
        annotations=READ_ONLY_TOOL,
    )
    def context_pack(
        symbol: str,
        purpose: str = "port-to-rust",
        max_text_chars: int = 300,
        neighbor_text: bool = True,
        max_type_edges: int = 20,
        max_type_observations: int = 5,
        type_depth: int = 1,
    ) -> Dict[str, Any]:
        return session.context_pack(
            symbol,
            purpose,
            max_text_chars,
            neighbor_text,
            max_type_edges,
            max_type_observations,
            type_depth,
        )

    @mcp.tool(
        name="snapshot_history",
        description="Bounded newest-first list of published snapshots for this graph.",
        annotations=READ_ONLY_TOOL,
    )
    def snapshot_history(limit: int = DEFAULT_HISTORY_LIMIT) -> Dict[str, Any]:
        return session.snapshot_history(limit)

    @mcp.tool(
        name="snapshot_diff",
        description="Structural persisted-row diff of two published snapshots.",
        annotations=READ_ONLY_TOOL,
    )
    def snapshot_diff(
        from_snapshot: str,
        to_snapshot: str = "current",
        max_items: int = DEFAULT_DIFF_MAX_ITEMS,
    ) -> Dict[str, Any]:
        return session.snapshot_diff(from_snapshot, to_snapshot, max_items)

    _forbid_unknown_tool_arguments(mcp)
    return mcp


def _forbid_unknown_tool_arguments(mcp: MCPServer) -> None:
    """Reject undeclared fields with the pinned SDK and advertise that schema."""
    for tool in mcp._tool_manager.list_tools():
        model = tool.fn_metadata.arg_model
        strict_model = type(
            model.__name__ + "Strict",
            (model,),
            {
                "model_config": ConfigDict(
                    extra="forbid",
                    arbitrary_types_allowed=True,
                )
            },
        )
        tool.fn_metadata.arg_model = strict_model
        tool.parameters = strict_model.model_json_schema(by_alias=True)


def resolve_graph_root(graph: Path) -> Path:
    path = Path(graph)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def preflight_graph(graph_root: Path, indexer: str) -> Dict[str, Any]:
    lang = indexer.strip().lower()
    if lang not in {"auto", "python", "c"}:
        raise SystemExit(f"graphrag-code mcp: unknown --indexer {indexer!r}; use auto, python, or c")
    try:
        report = audit_graph_root(
            graph_root,
            indexer=lang,
            allow_unlocked_managed=False,
        )
    except AmbiguousIndexerError as exc:
        print(f"graphrag-code mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except (PersistedGraphDoctorError, SnapshotGraphAuditError) as exc:
        print(f"graphrag-code mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"graphrag-code mcp: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if not report.get("ok"):
        status = report.get("status")
        n_anom = report.get("n_anomalies")
        print(
            f"graphrag-code mcp: persisted integrity is not ok "
            f"(status={status} anomalies={n_anom}). "
            "Refuse to serve an invalid or ambiguous graph.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    resolved = report.get("indexer")
    if resolved not in {"python", "c"}:
        print(
            "graphrag-code mcp: doctor did not resolve a concrete indexer",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return report


def build_session(graph: Path, indexer: str) -> GraphMcpSession:
    graph_root = resolve_graph_root(graph)
    report = preflight_graph(graph_root, indexer)
    return GraphMcpSession(
        graph_root,
        configured_indexer=indexer.strip().lower(),
        resolved_indexer=str(report["indexer"]),
        preflight=report,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only MCP stdio server for one BYOG graph."
    )
    parser.add_argument(
        "--graph",
        "-g",
        type=Path,
        required=True,
        help="BYOG graph root, relative to the invoking working directory.",
    )
    parser.add_argument(
        "--indexer",
        type=str,
        default="auto",
        choices=("auto", "python", "c"),
        help="python, c, or auto (fail closed if persisted evidence is ambiguous).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, force=True)
    session = build_session(args.graph, args.indexer)
    print(
        f"graphrag-code mcp: serving {session.graph_root} "
        f"indexer={session.resolved_indexer} (stdio, read-only)",
        file=sys.stderr,
    )
    build_mcp_server(session).run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
