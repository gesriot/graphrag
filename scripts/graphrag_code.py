#!/usr/bin/env python
"""Product CLI surface for the graphrag-code pipeline (Plan Phase 3).

One entry point over the existing scripts — not a rewrite. Every subcommand
delegates to behaviour already reachable via ``scripts/*.py``; those scripts
remain the stable API for ablation, check_port.sh, and other callers.

Usage:
    uv run python scripts/graphrag_code.py --help
    uv run python scripts/graphrag_code.py query-symbol parse_duration --graph byog_isodate
    uv run python scripts/graphrag_code.py context-pack isoduration:parse_duration \\
        --graph byog_isodate --purpose port-to-rust --json

What this is not: a demonstrated accuracy multiplier for cold porting. The
Phase 7 ablations showed no graph-vs-raw capability win on bounded library
slices; this CLI packages the *rails* (index, query, packs, audit, port_eval).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import typer

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent

# So ``from byog_graph import …`` works the same as the other scripts.
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=(
        "graphrag-code: deterministic code graph → query → context-pack → audit → port-eval. "
        "Thin wrapper over scripts/*.py; those scripts keep working unchanged."
    ),
)


def _delegate(script: str, args: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str] | int:
    """Run an existing scripts/*.py with the same interpreter; cwd = repo root."""
    cmd = [sys.executable, str(SCRIPTS / script), *args]
    if capture:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    raise SystemExit(subprocess.call(cmd, cwd=ROOT))


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _graph_opt() -> Any:
    return typer.Option(
        Path("byog_mini_game"),
        "--graph",
        "-g",
        help="BYOG graph root (snapshot layout with current + snapshots/).",
    )


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


@app.command("index-python")
def index_python(
    package: Path = typer.Option(..., "--package", "-p", help="Python package root to index"),
    graph: Path = typer.Option(..., "--graph", "-g", help="Target BYOG graph root"),
    keep_snapshots: int = typer.Option(5, "--keep-snapshots", "--keep-last"),
    use_advanced: bool = typer.Option(False, "--use-advanced", "--use-jedi-pyright"),
):
    """Index a Python package into a BYOG graph (delegates to index_python.py)."""
    args = ["--package", str(package), "--graph", str(graph), "--keep-snapshots", str(keep_snapshots)]
    if use_advanced:
        args.append("--use-advanced")
    _delegate("index_python.py", args)


@app.command("index-c")
def index_c(
    package: Path = typer.Option(..., "--package", "-p", help="C package/dir to index"),
    graph: Path = typer.Option(..., "--graph", "-g", help="Target BYOG graph root"),
    keep_snapshots: int = typer.Option(5, "--keep-snapshots", "--keep-last"),
):
    """Index a C tree into a BYOG graph (delegates to index_c.py)."""
    args = ["--package", str(package), "--graph", str(graph), "--keep-snapshots", str(keep_snapshots)]
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
):
    """Index a repo (dispatches to index-python or index-c by --lang)."""
    lang_n = lang.strip().lower()
    if lang_n in ("python", "py"):
        args = ["--package", str(package), "--graph", str(graph), "--keep-snapshots", str(keep_snapshots)]
        if use_advanced:
            args.append("--use-advanced")
        _delegate("index_python.py", args)
    elif lang_n == "c":
        if use_advanced:
            raise SystemExit("index --lang c does not support --use-advanced (that is Python-only)")
        args = ["--package", str(package), "--graph", str(graph), "--keep-snapshots", str(keep_snapshots)]
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
    json_out: bool = typer.Option(False, "--json", help="Machine-readable JSON (same shape as graph_query symbol)"),
):
    """Look up a symbol in the graph (delegates to graph_query.py symbol)."""
    # graph_query symbol already prints JSON on hit / "Not found" on miss.
    # --json keeps that machine shape; default is a short human view of the same data.
    res = _delegate(
        "graph_query.py",
        ["symbol", symbol, "--graph", str(graph)],
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
    json_out: bool = typer.Option(False, "--json"),
):
    """Who calls this symbol (graph_query.py callers)."""
    if not json_out:
        _delegate("graph_query.py", ["callers", symbol, "--graph", str(graph)])
        return
    from byog_graph import ByogGraph  # type: ignore

    _print_json(ByogGraph(graph).callers(symbol))


@app.command("callees")
def callees(
    symbol: str = typer.Argument(...),
    graph: Path = _graph_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """What this symbol calls (graph_query.py callees)."""
    if not json_out:
        _delegate("graph_query.py", ["callees", symbol, "--graph", str(graph)])
        return
    from byog_graph import ByogGraph  # type: ignore

    _print_json(ByogGraph(graph).callees(symbol))


@app.command("neighbors")
def neighbors(
    symbol: str = typer.Argument(...),
    graph: Path = _graph_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """Incoming/outgoing neighbors of a symbol (graph_query.py neighbors)."""
    if not json_out:
        _delegate("graph_query.py", ["neighbors", symbol, "--graph", str(graph)])
        return
    from byog_graph import ByogGraph  # type: ignore

    _print_json(ByogGraph(graph).neighbors(symbol))


@app.command("subgraph")
def subgraph(
    symbol: str = typer.Argument(..., help="Symbol or module"),
    graph: Path = _graph_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """Local subgraph around a symbol — same data as neighbors (no separate pipeline stage).

    Plan Phase 3 names this command; today it is exactly graph_query neighbors
    (incoming + outgoing edges). A multi-hop induced subgraph is not a distinct
    existing invocation, so this does not invent one.
    """
    if not json_out:
        _delegate("graph_query.py", ["neighbors", symbol, "--graph", str(graph)])
        return
    from byog_graph import ByogGraph  # type: ignore

    _print_json(ByogGraph(graph).neighbors(symbol))


@app.command("dependency-order")
def dependency_order(
    graph: Path = _graph_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """Containment-based dependency order (graph_query.py dependency-order)."""
    if not json_out:
        _delegate("graph_query.py", ["dependency-order", "--graph", str(graph)])
        return
    from byog_graph import ByogGraph  # type: ignore

    _print_json(ByogGraph(graph).dependency_order())


@app.command("impact")
def impact(
    symbol: str = typer.Argument(...),
    graph: Path = _graph_opt(),
    json_out: bool = typer.Option(False, "--json"),
):
    """Transitive callers (who would be affected) — graph_query.py impact."""
    if not json_out:
        _delegate("graph_query.py", ["impact", symbol, "--graph", str(graph)])
        return
    from byog_graph import ByogGraph  # type: ignore

    _print_json(ByogGraph(graph).impact(symbol))


@app.command("observations")
def observations(
    query: str = typer.Argument(..., help="Symbol or module"),
    graph: Path = _graph_opt(),
    json_out: bool = typer.Option(False, "--json", help="Same JSON shape as graph_query observations --json"),
):
    """Weak/ambiguous call observations (graph_query.py observations)."""
    args = ["observations", query, "--graph", str(graph)]
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
    purpose: str = typer.Option("port-to-rust", "--purpose", "-p"),
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Write full JSON pack to this path"),
    max_text_chars: int = typer.Option(300, "--max-text-chars"),
    full_text: bool = typer.Option(False, "--full-text"),
    neighbor_text: bool = typer.Option(True, "--neighbor-text/--no-neighbor-text"),
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
    args = [symbol, "--graph", str(graph), "--purpose", purpose, "--max-text-chars", str(max_text_chars)]
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
    if pack.get("uncertain_calls") is not None:
        print(f"uncertain   : {len(pack.get('uncertain_calls') or [])}")
    print("(full pack: re-run with --json)")


# ---------------------------------------------------------------------------
# Audit + port eval
# ---------------------------------------------------------------------------


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


@app.command("port-eval")
def port_eval(
    graph: Path = typer.Option(..., "--graph", "-g"),
    source: Path = typer.Option(Path("examples/mini_game"), "--source"),
    port: Path = typer.Option(Path("examples/mini_game_rust"), "--port"),
    target: Optional[str] = typer.Option(None, "--target"),
    symbol: list[str] = typer.Option([], "--symbol"),
    reindex: bool = typer.Option(False, "--reindex"),
    use_advanced: bool = typer.Option(False, "--use-advanced"),
    manual_fixes: int = typer.Option(0, "--manual-fixes"),
    skip_rust: bool = typer.Option(False, "--skip-rust"),
    json_out: bool = typer.Option(False, "--json", help="Same JSON shape as port_eval.py --json"),
    markdown: Optional[Path] = typer.Option(None, "--markdown"),
):
    """End-to-end port eval report (delegates to port_eval.py)."""
    args = ["--graph", str(graph), "--source", str(source), "--port", str(port), "--manual-fixes", str(manual_fixes)]
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
    _delegate("port_eval.py", args)


if __name__ == "__main__":
    app()
