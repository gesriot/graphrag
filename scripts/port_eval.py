#!/usr/bin/env python
"""
End-to-end Python->Rust port evaluation harness (no external API).

Where audit_call_edges.py measures the *graph* (the means), this measures the
*port* (the north-star: Python -> Rust working). It runs the full chain and
emits one repeatable report instead of a pass/crash loop:

    graph quality (audit_call_edges)  ->  context packs  ->  cargo fmt/check/test/run

Unlike agent_port_loop.py it never aborts on the first failure: every stage is
captured (ok / fail / skipped) so the report is comparable across runs and repos.

Example:
    uv run python scripts/port_eval.py \
        --source examples/mini_game --port examples/mini_game_rust --graph byog_mini_game
    uv run python scripts/port_eval.py --graph byog_mini_game --json > port_eval.json
    uv run python scripts/port_eval.py --graph byog_mini_game --markdown report.md
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

sys.path.insert(0, str(Path(__file__).parent))
from audit_call_edges import build_report  # noqa: E402
from byog_graph import ByogGraph  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: List[str], cwd: Path, timeout: int = 600) -> Dict[str, Any]:
    """Run a command, capturing status + output tail (never raises on non-zero)."""
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,  # so a CLI that reads stdin (cargo run) gets EOF, not a hang
        )
    except FileNotFoundError:
        return {"status": "skipped", "reason": f"{cmd[0]} not found", "cmd": " ".join(cmd)}
    except subprocess.TimeoutExpired:
        return {"status": "fail", "reason": "timeout", "cmd": " ".join(cmd)}
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-20:]
    return {
        "status": "ok" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "cmd": " ".join(cmd),
        "output_tail": tail,
    }


def eval_graph(graph: Path, source: Path, reindex: bool, use_advanced: bool) -> Dict[str, Any]:
    if reindex:
        cmd = [
            sys.executable, str(ROOT / "scripts" / "index_python.py"),
            "--package", str(source), "--graph", str(graph),
        ]
        if use_advanced:
            cmd.append("--use-advanced")
        _run(cmd, cwd=ROOT)
    report = build_report(graph, sample=0)
    g = ByogGraph(graph)
    s = report["structural"]
    return {
        "graph": report["graph"],
        "snapshot": report["snapshot"],
        "total_calls": report["total_calls"],
        "structural_pass_rate": s["pass_rate"],
        "structural_anomalies": s["anomaly_count"],
        "dangling_targets": report["dangling_count"],
        "observations": int(len(g.call_observations)),
        "clean": s["anomaly_count"] == 0 and report["dangling_count"] == 0,
    }


def default_key_symbols(graph: Path, n: int = 3) -> List[str]:
    """Pick the most-called symbols as 'key' symbols when none are given.

    Generic across projects: top targets by incoming call count that are real
    entities (functions/methods), so the harness is not mini_game-specific.
    """
    g = ByogGraph(graph)
    titles = set(g.ents["title"].astype(str))
    calls = g.rels[g.rels["type"].astype(str) == "calls"]
    counts = calls["target"].astype(str).value_counts()
    return [t for t in counts.index if t in titles][:n]


def gen_context_packs(symbols: List[str], graph: Path, out_dir: Path) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    pack_script = ROOT / "scripts" / "context_pack.py"
    generated: List[str] = []
    failed: List[str] = []
    for sym in symbols:
        out_file = out_dir / f"context_pack_{sym.replace(':', '_')}.json"
        res = _run(
            [
                sys.executable, str(pack_script), sym,
                "--graph", str(graph), "--purpose", "port-to-rust",
                "--max-text-chars", "0", "--output", str(out_file),
            ],
            cwd=ROOT,
        )
        (generated if res["status"] == "ok" and out_file.exists() else failed).append(sym)
    return {"requested": symbols, "generated": generated, "failed": failed, "count": len(generated)}


def eval_rust(port_dir: Path) -> Dict[str, Any]:
    if not (port_dir / "Cargo.toml").exists():
        return {"status": "skipped", "reason": f"no Cargo.toml in {port_dir}"}
    stages = {
        "fmt": ["cargo", "fmt", "--check"],
        "check": ["cargo", "check"],
        "golden_test": ["cargo", "test", "--test", "golden_contract", "--", "--quiet"],
        "run": ["cargo", "run", "--quiet"],
    }
    results = {name: _run(cmd, cwd=port_dir) for name, cmd in stages.items()}
    results["all_ok"] = all(r.get("status") == "ok" for r in results.values())
    return results


def count_golden(source: Path) -> Dict[str, Any]:
    tests_dir = source / "tests"
    files = sorted(tests_dir.glob("golden_*.json")) if tests_dir.exists() else []
    names = [p.name for p in files]
    case_counts: Dict[str, int] = {}
    for p in files:
        data = json.loads(p.read_text())
        cases = data.get("cases")
        # mini_lang groups many golden cases per file; mini_game uses one trace
        # per file. Count the actual behavior cases when present, otherwise
        # count the file as a single golden scenario.
        case_counts[p.name] = len(cases) if isinstance(cases, list) else 1
    return {
        "count": sum(case_counts.values()),
        "file_count": len(names),
        "names": names,
        "case_counts": case_counts,
    }


def build_eval_report(
    source: Path,
    port_dir: Path,
    graph: Path,
    target: str,
    symbols: List[str],
    reindex: bool,
    use_advanced: bool,
    manual_fixes: int,
    skip_rust: bool,
) -> Dict[str, Any]:
    graph_res = eval_graph(graph, source, reindex, use_advanced)
    if not symbols:
        symbols = default_key_symbols(graph)
    packs = gen_context_packs(symbols, graph, ROOT / "output" / "port_eval" / target)
    rust = {"status": "skipped", "reason": "--skip-rust"} if skip_rust else eval_rust(port_dir)
    golden = count_golden(source)

    rust_ok = rust.get("all_ok", False)
    golden_passed = rust.get("golden_test", {}).get("status") == "ok" if not skip_rust else None
    # Without the cargo stages we cannot assert the end-to-end (north-star) result.
    overall = None if skip_rust else bool(graph_res["clean"] and rust_ok and golden_passed)

    return {
        "target": target,
        "graph": graph_res,
        "context_packs": packs,
        "rust": rust,
        "golden_scenarios": {**golden, "passed": golden_passed},
        "manual_fix_count": manual_fixes,
        "overall_pass": overall,
    }


def to_markdown(r: Dict[str, Any]) -> str:
    g = r["graph"]
    lines = [
        f"# Port eval: {r['target']}",
        "",
        "## Graph (means)",
        f"- structural pass rate: **{g['structural_pass_rate']}** "
        f"(anomalies={g['structural_anomalies']}, dangling={g['dangling_targets']})",
        f"- calls: {g['total_calls']}  |  observations: {g['observations']}  |  clean: {g['clean']}",
        "",
        "## Context packs",
        f"- generated {r['context_packs']['count']}/{len(r['context_packs']['requested'])}: "
        f"{', '.join(r['context_packs']['generated']) or '-'}",
        "",
        "## Rust (end-to-end / north-star)",
    ]
    rust = r["rust"]
    if rust.get("status") == "skipped":
        lines.append(f"- skipped ({rust.get('reason')})")
    else:
        for stage in ("fmt", "check", "golden_test", "run"):
            lines.append(f"- {stage}: **{rust.get(stage, {}).get('status', '?')}**")
    gs = r["golden_scenarios"]
    lines += [
        "",
        "## Golden cases",
        f"- {gs['count']} cases/scenarios across {gs.get('file_count', len(gs['names']))} files, passed: {gs['passed']}",
        "",
        f"**manual_fix_count: {r['manual_fix_count']}**",
        f"**OVERALL PASS: {r['overall_pass']}**",
    ]
    return "\n".join(lines)


def main(
    graph: Path = typer.Option(..., "--graph", help="BYOG graph root (e.g. byog_mini_game)"),
    source: Path = typer.Option(Path("examples/mini_game"), "--source", help="Python source project"),
    port: Path = typer.Option(Path("examples/mini_game_rust"), "--port", help="Rust port (Cargo project)"),
    target: Optional[str] = typer.Option(None, "--target", help="Logical target name (default: source dir name)"),
    symbol: List[str] = typer.Option(
        [], "--symbol",
        help="Key symbol(s)/module(s) to context-pack (repeatable). If omitted, the most-called symbols are auto-selected.",
    ),
    reindex: bool = typer.Option(False, "--reindex", help="Regenerate the graph from --source via index_python first"),
    use_advanced: bool = typer.Option(False, "--use-advanced", help="Advanced resolver when reindexing"),
    manual_fixes: int = typer.Option(0, "--manual-fixes", help="Manual interventions needed (recorded, set by hand for now)"),
    skip_rust: bool = typer.Option(False, "--skip-rust", help="Skip cargo stages (graph + packs only)"),
    json_output: bool = typer.Option(False, "--json", help="Emit full machine-readable report"),
    markdown: Optional[Path] = typer.Option(None, "--markdown", help="Also write a Markdown report to this path"),
) -> None:
    """Run the Python->Rust port eval and emit a repeatable report."""
    report = build_eval_report(
        source=source if source.is_absolute() else ROOT / source,
        port_dir=port if port.is_absolute() else ROOT / port,
        graph=graph,
        target=target or source.name,
        symbols=symbol,
        reindex=reindex,
        use_advanced=use_advanced,
        manual_fixes=manual_fixes,
        skip_rust=skip_rust,
    )
    if markdown is not None:
        markdown.write_text(to_markdown(report))
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
        return

    g = report["graph"]
    print(f"target            : {report['target']}")
    print(f"graph pass rate   : {g['structural_pass_rate']} "
          f"(anomalies={g['structural_anomalies']}, dangling={g['dangling_targets']}, "
          f"calls={g['total_calls']}, obs={g['observations']})")
    print(f"context packs     : {report['context_packs']['count']}/{len(report['context_packs']['requested'])} "
          f"{report['context_packs']['generated']}")
    rust = report["rust"]
    if rust.get("status") == "skipped":
        print(f"rust              : skipped ({rust.get('reason')})")
    else:
        print("rust              : " + "  ".join(
            f"{s}={rust.get(s, {}).get('status', '?')}" for s in ("fmt", "check", "golden_test", "run")))
    gs = report["golden_scenarios"]
    print(
        f"golden cases      : {gs['count']} across {gs.get('file_count', len(gs['names']))} files "
        f"(passed={gs['passed']})"
    )
    print(f"manual fixes      : {report['manual_fix_count']}")
    print(f"OVERALL PASS      : {report['overall_pass']}")
    if markdown is not None:
        print(f"(markdown written : {markdown})")


if __name__ == "__main__":
    typer.run(main)
