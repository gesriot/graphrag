#!/usr/bin/env python
"""Product CLI surface for the graphrag-code pipeline (Plan Phase 3).

One entry point over the existing scripts — not a rewrite. Every subcommand
delegates to behaviour already reachable via ``scripts/*.py``; those scripts
remain the stable API for ablation, check_port.sh, and other callers.

Usage:
    graphrag-code --help
    python -m graphrag_code --help
    uv run python scripts/graphrag_code.py --help

What this is not: a demonstrated accuracy multiplier for cold porting. The
Phase 7 ablations showed no graph-vs-raw capability win on bounded library
slices; this CLI packages the *rails* (index, query, packs, audit,
adopt-publication-lock, snapshot-history, snapshot-diff, snapshot-activate,
port_eval).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from graphrag_code.byog_graph import (
    DEFAULT_TYPE_CLOSURE_MAX_DEPTH,
    DEFAULT_TYPE_CLOSURE_MAX_EDGES,
    DEFAULT_TYPE_CLOSURE_MAX_NODES,
)
from graphrag_code.paths import source_checkout_root

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "graphrag-code: deterministic code graph → query → context-pack → "
        "persisted doctor → adopt-publication-lock → snapshot-history → "
        "snapshot-diff → snapshot-activate → port-eval. Relative "
        "paths are resolved from the invoking working directory."
    ),
)

_DELEGATE_MODULES = {
    "index_python.py": "graphrag_code.index_python",
    "index_c.py": "graphrag_code.index_c",
    "graph_query.py": "graphrag_code.graph_query",
    "context_pack.py": "graphrag_code.context_pack",
    "persisted_graph_doctor.py": "graphrag_code.persisted_graph_doctor",
    "adopt_publication_lock.py": "graphrag_code.adopt_publication_lock",
    "snapshot_compare.py": "graphrag_code.snapshot_compare",
    "snapshot_activate.py": "graphrag_code.snapshot_activate",
    "audit_call_edges.py": "graphrag_code.audit_call_edges",
    "port_eval.py": "graphrag_code.port_eval",
}


def _child_env() -> dict[str, str] | None:
    """Keep checkout ``python scripts/*.py`` delegation importable without chdir.

    Installed wheels leave ``PYTHONPATH`` untouched. A src-layout checkout may
    add only ``<root>/src`` so ``python -m graphrag_code.*`` resolves the same
    tree the parent shim already imported.
    """
    root = source_checkout_root()
    if root is None:
        return None
    env = os.environ.copy()
    src = str(root / "src")
    current = env.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if src not in parts:
        env["PYTHONPATH"] = src + ((os.pathsep + current) if current else "")
    return env


def _delegate(script: str, args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str] | int:
    """Run a packaged module with the same interpreter. Does not change cwd."""
    module = _DELEGATE_MODULES.get(script)
    if module is None:
        raise SystemExit(f"graphrag-code: unknown delegated command {script}")
    cmd = [sys.executable, "-m", module, *args]
    env = _child_env()
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True, env=env)
    raise SystemExit(subprocess.call(cmd, env=env))


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _graph_opt() -> Any:
    return typer.Option(
        Path("byog_mini_game"),
        "--graph",
        "-g",
        help="BYOG graph root, relative to the invoking working directory.",
    )


def _snapshot_opt() -> Any:
    return typer.Option(
        None,
        "--snapshot",
        help=(
            "published snapshot id or 'current'. Omit for the default current "
            "snapshot, or the legacy flat-parquet layout. Explicit ids never "
            "change current. Unlocked compatibility reads have no retention "
            "guarantee."
        ),
    )


def _append_snapshot(args: list[str], snapshot: Optional[str]) -> list[str]:
    if snapshot is not None:
        args.extend(["--snapshot", snapshot])
    return args


def _json_query(graph: Path, snapshot: Optional[str], fn) -> None:
    from graphrag_code.snapshot_read import SnapshotReadError, retained_snapshot_read

    try:
        with retained_snapshot_read(
            graph, snapshot, allow_unlocked_managed=True
        ) as scope:
            _print_json(fn(scope.load_graph()))
    except SnapshotReadError as error:
        sys.stderr.write(f"graphrag-code: {error}\n")
        raise SystemExit(error.exit_code) from error


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@app.command("index-python")
def index_python(
    package: Path = typer.Option(..., "--package", "-p", help="Python package root to index"),
    graph: Path = typer.Option(..., "--graph", "-g", help="Target BYOG graph root"),
    keep_snapshots: int = typer.Option(5, "--keep-snapshots", "--keep-last"),
    use_advanced: bool = typer.Option(False, "--use-advanced", "--use-jedi-pyright"),
    reuse_unchanged: bool = typer.Option(
        False,
        "--reuse-unchanged",
        help="Reuse the current snapshot when supported deterministic inputs are unchanged.",
    ),
):
    """Index a Python package into a BYOG graph (delegates to index_python.py)."""
    args = ["--package", str(package), "--graph", str(graph), "--keep-snapshots", str(keep_snapshots)]
    if use_advanced:
        args.append("--use-advanced")
    if reuse_unchanged:
        args.append("--reuse-unchanged")
    _delegate("index_python.py", args)


@app.command("index-c")
def index_c(
    package: Path = typer.Option(..., "--package", "-p", help="C package/dir to index"),
    graph: Path = typer.Option(..., "--graph", "-g", help="Target BYOG graph root"),
    keep_snapshots: int = typer.Option(5, "--keep-snapshots", "--keep-last"),
    reuse_unchanged: bool = typer.Option(
        False,
        "--reuse-unchanged",
        help="Reuse the current snapshot when supported deterministic inputs are unchanged.",
    ),
):
    """Index a C tree into a BYOG graph (delegates to index_c.py)."""
    args = ["--package", str(package), "--graph", str(graph), "--keep-snapshots", str(keep_snapshots)]
    if reuse_unchanged:
        args.append("--reuse-unchanged")
    _delegate("index_c.py", args)


@app.command("index")
def index(
    package: Path = typer.Option(..., "--package", "-p", help="Source package/dir to index"),
    graph: Path = typer.Option(..., "--graph", "-g", help="Target BYOG graph root"),
    lang: str = typer.Option(..., "--lang", help="Source language: python | c"),
    keep_snapshots: int = typer.Option(5, "--keep-snapshots", "--keep-last"),
    use_advanced: bool = typer.Option(
        False, "--use-advanced", help="Python only: enable Jedi/Pyright advanced resolution"
    ),
    reuse_unchanged: bool = typer.Option(
        False,
        "--reuse-unchanged",
        help="Reuse the current snapshot when supported deterministic inputs are unchanged.",
    ),
):
    """Index a repo (dispatches to index-python or index-c by --lang)."""
    lang_n = lang.strip().lower()
    if lang_n in ("python", "py"):
        args = ["--package", str(package), "--graph", str(graph), "--keep-snapshots", str(keep_snapshots)]
        if use_advanced:
            args.append("--use-advanced")
        if reuse_unchanged:
            args.append("--reuse-unchanged")
        _delegate("index_python.py", args)
    elif lang_n == "c":
        if use_advanced:
            raise SystemExit("index --lang c does not support --use-advanced (that is Python-only)")
        args = ["--package", str(package), "--graph", str(graph), "--keep-snapshots", str(keep_snapshots)]
        if reuse_unchanged:
            args.append("--reuse-unchanged")
        _delegate("index_c.py", args)
    else:
        raise SystemExit(f"unknown --lang {lang!r}; use python or c")


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


@app.command("query-symbol")
def query_symbol(
    symbol: str = typer.Argument(..., help="Symbol title or partial (e.g. parse_duration)"),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON (same shape as graph_query symbol)"),
):
    """Look up a symbol in the graph (delegates to graph_query.py symbol)."""
    # graph_query symbol already prints JSON on hit / "Not found" on miss.
    # --json keeps that machine shape; default is a short human view of the same data.
    res = _delegate(
        "graph_query.py",
        _append_snapshot(["symbol", symbol, "--graph", str(graph)], snapshot),
        capture=True,
    )
    assert isinstance(res, subprocess.CompletedProcess)
    out = res.stdout.rstrip("\n")
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        raise SystemExit(res.returncode)
    if out == "Not found" or not out.strip():
        # graph_query.py prints "Not found" and exits 0; keep that contract.
        if json_out:
            _print_json(None)
        else:
            print("Not found")
        return
    data = json.loads(out)
    if json_out:
        # Identical field names/shape to graph_query.py symbol.
        _print_json(data)
        return
    print(f"title       : {data.get('title')}")
    print(f"type        : {data.get('type')}")
    print(f"source_file : {data.get('source_file')}")
    print(f"span        : {data.get('span')}")
    desc = data.get("description") or ""
    if desc:
        first = str(desc).strip().splitlines()[0][:120]
        print(f"description : {first}")
    if data.get("snippet_preview"):
        print(f"snippet     : {str(data['snippet_preview'])[:100]}…")


@app.command("callers")
def callers(
    symbol: str = typer.Argument(...),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """Who calls this symbol (graph_query.py callers)."""
    if not json_out:
        _delegate(
            "graph_query.py",
            _append_snapshot(["callers", symbol, "--graph", str(graph)], snapshot),
        )
        return
    _json_query(graph, snapshot, lambda g: g.callers(symbol))


@app.command("callees")
def callees(
    symbol: str = typer.Argument(...),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """What this symbol calls (graph_query.py callees)."""
    if not json_out:
        _delegate(
            "graph_query.py",
            _append_snapshot(["callees", symbol, "--graph", str(graph)], snapshot),
        )
        return
    _json_query(graph, snapshot, lambda g: g.callees(symbol))


@app.command("types-used-by")
def types_used_by(
    symbol: str = typer.Argument(...),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """Outgoing uses_type targets (graph_query.py types-used-by)."""
    args = _append_snapshot(["types-used-by", symbol, "--graph", str(graph)], snapshot)
    if json_out:
        args.append("--json")
    _delegate("graph_query.py", args)


@app.command("type-users")
def type_users(
    symbol: str = typer.Argument(...),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """Incoming uses_type sources (graph_query.py type-users)."""
    args = _append_snapshot(["type-users", symbol, "--graph", str(graph)], snapshot)
    if json_out:
        args.append("--json")
    _delegate("graph_query.py", args)


@app.command("type-closure")
def type_closure(
    symbol: str = typer.Argument(...),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    direction: str = typer.Option(
        "dependencies",
        "--direction",
        help="dependencies (outgoing), users (incoming), or both",
    ),
    max_depth: int = typer.Option(DEFAULT_TYPE_CLOSURE_MAX_DEPTH, "--max-depth"),
    max_nodes: int = typer.Option(DEFAULT_TYPE_CLOSURE_MAX_NODES, "--max-nodes"),
    max_edges: int = typer.Option(DEFAULT_TYPE_CLOSURE_MAX_EDGES, "--max-edges"),
    json_out: bool = typer.Option(False, "--json"),
):
    """Bounded cycle-safe transitive uses_type closure (graph_query.py type-closure)."""
    args = _append_snapshot(
        [
            "type-closure",
            symbol,
            "--graph",
            str(graph),
            "--direction",
            direction,
            "--max-depth",
            str(max_depth),
            "--max-nodes",
            str(max_nodes),
            "--max-edges",
            str(max_edges),
        ],
        snapshot,
    )
    if json_out:
        args.append("--json")
    _delegate("graph_query.py", args)


@app.command("neighbors")
def neighbors(
    symbol: str = typer.Argument(...),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """Incoming/outgoing neighbors of a symbol (graph_query.py neighbors)."""
    if not json_out:
        _delegate(
            "graph_query.py",
            _append_snapshot(["neighbors", symbol, "--graph", str(graph)], snapshot),
        )
        return
    _json_query(graph, snapshot, lambda g: g.neighbors(symbol))


@app.command("subgraph")
def subgraph(
    symbol: str = typer.Argument(..., help="Symbol or module"),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """Local subgraph around a symbol — same data as neighbors (no separate pipeline stage).

    Plan Phase 3 names this command; today it is exactly graph_query neighbors
    (incoming + outgoing edges). A multi-hop induced subgraph is not a distinct
    existing invocation, so this does not invent one.
    """
    if not json_out:
        _delegate(
            "graph_query.py",
            _append_snapshot(["neighbors", symbol, "--graph", str(graph)], snapshot),
        )
        return
    _json_query(graph, snapshot, lambda g: g.neighbors(symbol))


@app.command("dependency-order")
def dependency_order(
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """Containment-based dependency order (graph_query.py dependency-order)."""
    if not json_out:
        _delegate(
            "graph_query.py",
            _append_snapshot(["dependency-order", "--graph", str(graph)], snapshot),
        )
        return
    _json_query(graph, snapshot, lambda g: g.dependency_order())


@app.command("impact")
def impact(
    symbol: str = typer.Argument(...),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """Transitive callers (who would be affected) — graph_query.py impact."""
    if not json_out:
        _delegate(
            "graph_query.py",
            _append_snapshot(["impact", symbol, "--graph", str(graph)], snapshot),
        )
        return
    _json_query(graph, snapshot, lambda g: g.impact(symbol))


@app.command("observations")
def observations(
    query: str = typer.Argument(..., help="Symbol or module"),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    json_out: bool = typer.Option(False, "--json", help="Same JSON shape as graph_query observations --json"),
):
    """Weak/ambiguous call observations (graph_query.py observations)."""
    args = _append_snapshot(["observations", query, "--graph", str(graph)], snapshot)
    if json_out:
        args.append("--json")
    _delegate("graph_query.py", args)


# ---------------------------------------------------------------------------
# Context pack
# ---------------------------------------------------------------------------


@app.command("context-pack")
def context_pack(
    symbol: str = typer.Argument(..., help="Symbol or module title"),
    graph: Path = _graph_opt(),
    snapshot: Optional[str] = _snapshot_opt(),
    purpose: str = typer.Option("port-to-rust", "--purpose", "-p"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write full JSON pack to this path"),
    max_text_chars: int = typer.Option(300, "--max-text-chars"),
    full_text: bool = typer.Option(False, "--full-text"),
    neighbor_text: bool = typer.Option(True, "--neighbor-text/--no-neighbor-text"),
    max_type_edges: int = typer.Option(
        20,
        "--max-type-edges",
        help=(
            "Max uses_type edges per direction and, for transitive sections, "
            "returned closure-node payloads (default 20)"
        ),
    ),
    max_type_observations: int = typer.Option(
        5,
        "--max-type-observations",
        help="Max observations sampled per uses_type edge (default 5)",
    ),
    type_depth: int = typer.Option(
        1,
        "--type-depth",
        help="uses_type depth (default 1 = direct only; >1 adds type_*_closure)",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Full pack JSON on stdout (identical to scripts/context_pack.py default output)",
    ),
):
    """Assemble a context pack for a symbol (delegates to context_pack.py).

    Default: short human summary. --json: exact pack JSON from the underlying
    script (field names unchanged).
    """
    args = _append_snapshot(
        [
            symbol,
            "--graph",
            str(graph),
            "--purpose",
            purpose,
            "--max-text-chars",
            str(max_text_chars),
            "--max-type-edges",
            str(max_type_edges),
            "--max-type-observations",
            str(max_type_observations),
            "--type-depth",
            str(type_depth),
        ],
        snapshot,
    )
    if full_text:
        args.append("--full-text")
    if not neighbor_text:
        args.append("--no-neighbor-text")
    if output is not None:
        # Write path: underlying script handles file + "Wrote …" message.
        args.extend(["--output", str(output)])
        _delegate("context_pack.py", args)
        return

    res = _delegate("context_pack.py", args, capture=True)
    assert isinstance(res, subprocess.CompletedProcess)
    if res.returncode != 0:
        sys.stderr.write(res.stderr)
        # context_pack may also print help on stderr; forward stdout too if any
        if res.stdout:
            sys.stderr.write(res.stdout)
        raise SystemExit(res.returncode)
    raw = res.stdout
    if json_out:
        # Preserve exact bytes/shape from context_pack.py (already pretty JSON).
        sys.stdout.write(raw if raw.endswith("\n") else raw + "\n")
        return
    pack = json.loads(raw)
    print(f"symbol      : {pack.get('symbol')}")
    print(f"purpose     : {pack.get('purpose')}")
    ent = pack.get("entity") or {}
    print(f"type        : {ent.get('type')}")
    print(f"source_file : {ent.get('source_file')}")
    print(f"span        : {ent.get('span')}")
    print(f"neighbors   : {len(pack.get('neighbors') or [])}")
    print(f"text_units  : {len(pack.get('text_units') or [])}")
    if pack.get("data_dependencies") is not None:
        print(f"data_deps   : {len(pack.get('data_dependencies') or [])}")
    if pack.get("type_dependencies") is not None:
        print(f"type_deps   : {len(pack.get('type_dependencies') or [])}")
    if pack.get("type_user_edges") is not None:
        print(f"type_users  : {len(pack.get('type_user_edges') or [])}")
    if pack.get("type_dependency_closure") is not None:
        tdc = pack["type_dependency_closure"] or {}
        print(
            f"type_dep_cl : nodes={tdc.get('n_nodes_returned')}/"
            f"{tdc.get('n_nodes_total')} edges={tdc.get('n_edges_returned')}/"
            f"{tdc.get('n_edges_total')} depth={tdc.get('max_depth')}"
        )
    if pack.get("type_user_closure") is not None:
        tuc = pack["type_user_closure"] or {}
        print(
            f"type_usr_cl : nodes={tuc.get('n_nodes_returned')}/"
            f"{tuc.get('n_nodes_total')} edges={tuc.get('n_edges_returned')}/"
            f"{tuc.get('n_edges_total')} depth={tuc.get('max_depth')}"
        )
    if pack.get("uncertain_calls") is not None:
        print(f"uncertain   : {len(pack.get('uncertain_calls') or [])}")
    print("(full pack: re-run with --json)")


# ---------------------------------------------------------------------------
# Audit + port eval
# ---------------------------------------------------------------------------


@app.command("doctor")
def doctor(
    graph: Path = typer.Option(..., "--graph", "-g", help="BYOG graph root"),
    indexer: str = typer.Option(
        ...,
        "--indexer",
        help="python, c, or auto (fail closed if persisted evidence is ambiguous)",
    ),
    snapshot: Optional[str] = typer.Option(
        None, "--snapshot", help="audit this snapshot id instead of current"
    ),
    output: Optional[Path] = typer.Option(
        None, "--output", "-o", help="write the deterministic JSON report"
    ),
    json_out: bool = typer.Option(
        False, "--json", help="same JSON shape as persisted_graph_doctor.py --json"
    ),
    max_anomaly_samples: int = typer.Option(40, "--max-anomaly-samples"),
):
    """Read-only persisted-integrity doctor (delegates to persisted_graph_doctor.py).

    Validates the selected snapshot envelope and every applicable overlay
    contract without invoking an extractor, compiler, or publisher.
    """
    lang = indexer.strip().lower()
    args = [
        "--graph",
        str(graph),
        "--indexer",
        lang,
        "--max-anomaly-samples",
        str(max_anomaly_samples),
    ]
    if snapshot is not None:
        args.extend(["--snapshot", snapshot])
    if output is not None:
        args.extend(["--output", str(output)])
    if json_out:
        args.append("--json")
    _delegate("persisted_graph_doctor.py", args)


@app.command("adopt-publication-lock")
def adopt_publication_lock(
    graph: Path = typer.Option(..., "--graph", "-g", help="Managed BYOG graph root"),
    indexer: str = typer.Option(
        ...,
        "--indexer",
        help="python, c, or auto (fail closed if persisted evidence is ambiguous)",
    ),
    offline_confirmed: bool = typer.Option(
        False,
        "--offline-confirmed",
        help=(
            "Required to create .publish.lock. Asserts that no legacy reader "
            "that ignores .publish.lock is active, no legacy publisher or "
            "retention process that ignores .publish.lock is active, and "
            "future publishers will use the current lock-aware protocol. "
            "This program cannot prove those conditions."
        ),
    ),
    json_out: bool = typer.Option(
        False, "--json", help="same JSON shape as adopt_publication_lock.py --json"
    ),
):
    """Adopt .publish.lock on a pre-lock managed graph without rewriting payload.

    Explicit offline migration, never an automatic MCP or doctor side effect.
    Creates only <graph>/.publish.lock. Does not reindex, extract, publish,
    or alter current/snapshots.
    """
    lang = indexer.strip().lower()
    args = ["--graph", str(graph), "--indexer", lang]
    if offline_confirmed is True:
        args.append("--offline-confirmed")
    if json_out is True:
        args.append("--json")
    _delegate("adopt_publication_lock.py", args)


@app.command("snapshot-history")
def snapshot_history(
    graph: Path = typer.Option(..., "--graph", "-g", help="Managed BYOG graph root"),
    limit: int = typer.Option(
        20,
        "--limit",
        help="maximum published snapshots to return (default 20, hard max 200)",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="same JSON shape as snapshot_compare.py history --json"
    ),
    allow_unlocked_legacy: bool = typer.Option(
        False,
        "--allow-unlocked-legacy",
        help=(
            "Explicit read-only compatibility for immutable pre-lock evidence. "
            "Never creates .publish.lock and provides no retention guarantee."
        ),
    ),
):
    """List published snapshots newest-first under a shared reader lease.

    Staging directories are notices, not history. Does not reindex, publish,
    or create .publish.lock. Missing lock exits 2 unless
    --allow-unlocked-legacy is set.
    """
    args = ["history", "--graph", str(graph), "--limit", str(limit)]
    if json_out is True:
        args.append("--json")
    if allow_unlocked_legacy is True:
        args.append("--allow-unlocked-legacy")
    _delegate("snapshot_compare.py", args)


@app.command("snapshot-diff")
def snapshot_diff(
    graph: Path = typer.Option(..., "--graph", "-g", help="Managed BYOG graph root"),
    from_snapshot: str = typer.Option(
        ..., "--from", help="published snapshot id or 'current'"
    ),
    to_snapshot: str = typer.Option(
        ..., "--to", help="published snapshot id or 'current'"
    ),
    max_items: int = typer.Option(
        50,
        "--max-items",
        help="sample cap per added/removed/modified category per table (default 50, hard max 500)",
    ),
    json_out: bool = typer.Option(
        False, "--json", help="same JSON shape as snapshot_compare.py diff --json"
    ),
    allow_unlocked_legacy: bool = typer.Option(
        False,
        "--allow-unlocked-legacy",
        help=(
            "Explicit read-only compatibility for immutable pre-lock evidence. "
            "Never creates .publish.lock and provides no retention guarantee."
        ),
    ),
):
    """Structurally compare two published snapshots under a shared reader lease.

    Row modification means canonical persisted fields differ. This is not
    semantic equivalence. Does not reindex, publish, or create .publish.lock.
    """
    args = [
        "diff",
        "--graph",
        str(graph),
        "--from",
        from_snapshot,
        "--to",
        to_snapshot,
        "--max-items",
        str(max_items),
    ]
    if json_out is True:
        args.append("--json")
    if allow_unlocked_legacy is True:
        args.append("--allow-unlocked-legacy")
    _delegate("snapshot_compare.py", args)


@app.command("snapshot-activate")
def snapshot_activate(
    graph: Path = typer.Option(..., "--graph", "-g", help="Managed BYOG graph root"),
    snapshot: str = typer.Option(
        ...,
        "--snapshot",
        help="canonical published snapshot id to activate (not 'current')",
    ),
    expected_current: str = typer.Option(
        ...,
        "--expected-current",
        help="canonical published id that current must still name",
    ),
    activate_confirmed: bool = typer.Option(
        False,
        "--activate-confirmed",
        help=(
            "Required to change current. Explicit operator confirmation that "
            "this should activate a retained published snapshot. The command "
            "still refuses to write if current no longer matches "
            "--expected-current. Never an MCP tool."
        ),
    ),
    json_out: bool = typer.Option(
        False, "--json", help="same JSON shape as snapshot_activate.py --json"
    ),
):
    """Activate a retained published snapshot by changing only current.

    Requires --activate-confirmed and --expected-current. Does not delete,
    retain, publish, repair, or reindex. Requires an already-adopted
    .publish.lock and never creates that file. Intentionally absent from MCP.
    """
    args = [
        "--graph",
        str(graph),
        "--snapshot",
        snapshot,
        "--expected-current",
        expected_current,
    ]
    if activate_confirmed is True:
        args.append("--activate-confirmed")
    if json_out is True:
        args.append("--json")
    _delegate("snapshot_activate.py", args)


@app.command("audit-graph")
def audit_graph(
    graph: Path = typer.Option(..., "--graph", "-g", help="BYOG graph root"),
    source_root: Optional[Path] = typer.Option(None, "--source-root"),
    sample: int = typer.Option(30, "--sample"),
    seed: int = typer.Option(42, "--seed"),
    json_out: bool = typer.Option(False, "--json", help="Same JSON shape as audit_call_edges.py --json"),
):
    """Audit CALLS edges (delegates to audit_call_edges.py)."""
    args = ["--graph", str(graph), "--sample", str(sample), "--seed", str(seed)]
    if source_root is not None:
        args.extend(["--source-root", str(source_root)])
    if json_out:
        args.append("--json")
    _delegate("audit_call_edges.py", args)


@app.command("mcp")
def mcp(
    graph: Path = typer.Option(..., "--graph", "-g", help="BYOG graph root"),
    indexer: str = typer.Option(
        "auto",
        "--indexer",
        help="python, c, or auto (fail closed if persisted evidence is ambiguous)",
    ),
):
    """Read-only MCP stdio server for one existing BYOG graph.

    stdout is protocol traffic only. Diagnostics go to stderr. The process
    does not index, publish, or run port-eval.
    """
    lang = indexer.strip().lower()
    if lang not in {"auto", "python", "c"}:
        raise SystemExit(f"unknown --indexer {indexer!r}; use auto, python, or c")
    cmd = [
        sys.executable,
        "-m",
        "graphrag_code.mcp_server",
        "--graph",
        str(graph),
        "--indexer",
        lang,
    ]
    raise SystemExit(subprocess.call(cmd, env=_child_env()))


@app.command("port-eval")
def port_eval(
    graph: Optional[Path] = typer.Option(None, "--graph", "-g"),
    source: Optional[Path] = typer.Option(None, "--source"),
    port: Optional[Path] = typer.Option(None, "--port"),
    target: Optional[str] = typer.Option(None, "--target"),
    symbol: list[str] = typer.Option([], "--symbol"),
    reindex: bool = typer.Option(False, "--reindex"),
    use_advanced: bool = typer.Option(False, "--use-advanced"),
    manual_fixes: int = typer.Option(0, "--manual-fixes"),
    skip_rust: bool = typer.Option(False, "--skip-rust"),
    json_out: bool = typer.Option(False, "--json", help="Same JSON shape as port_eval.py --json"),
    markdown: Optional[Path] = typer.Option(None, "--markdown"),
    all_gates: bool = typer.Option(False, "--all-gates"),
    gate: Optional[str] = typer.Option(None, "--gate"),
    full: bool = typer.Option(False, "--full"),
):
    """End-to-end port eval. ``--all-gates`` is source-checkout only."""
    args = ["--manual-fixes", str(manual_fixes)]
    if graph is not None:
        args.extend(["--graph", str(graph)])
    if source is not None:
        args.extend(["--source", str(source)])
    if port is not None:
        args.extend(["--port", str(port)])
    if target is not None:
        args.extend(["--target", target])
    for s in symbol:
        args.extend(["--symbol", s])
    if reindex:
        args.append("--reindex")
    if use_advanced:
        args.append("--use-advanced")
    if skip_rust:
        args.append("--skip-rust")
    if json_out:
        args.append("--json")
    if markdown is not None:
        args.extend(["--markdown", str(markdown)])
    if all_gates:
        args.append("--all-gates")
    if gate is not None:
        args.extend(["--gate", gate])
    if full:
        args.append("--full")
    _delegate("port_eval.py", args)


def main() -> None:
    """Console-script and ``python -m graphrag_code`` entry."""
    app()


if __name__ == "__main__":
    main()
