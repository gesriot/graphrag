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
    uv run python scripts/port_eval.py --all-gates --full

The ``--gate``/``--all-gates`` mode reads ``scripts/port_gates.json``. It adds
fresh graph indexing, source-oracle checks, optional-tool skip semantics, and
non-zero exit status for a failed profile around the existing port-eval core.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer

sys.path.insert(0, str(Path(__file__).parent))
from audit_call_edges import build_report  # noqa: E402
from byog_graph import ByogGraph  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GATE_MANIFEST = ROOT / "scripts" / "port_gates.json"


def _run(
    cmd: List[str], cwd: Path, timeout: int = 600, env: Optional[Dict[str, str]] = None
) -> Dict[str, Any]:
    """Run a command, capturing status + output tail (never raises on non-zero)."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL,  # so a CLI that reads stdin (cargo run) gets EOF, not a hang
            env=None if env is None else {**os.environ, **env},
        )
    except FileNotFoundError:
        return {
            "status": "skipped",
            "reason": f"{cmd[0]} not found",
            "cmd": " ".join(cmd),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "fail",
            "reason": "timeout",
            "cmd": " ".join(cmd),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
    tail = (proc.stdout + proc.stderr).strip().splitlines()[-20:]
    return {
        "status": "ok" if proc.returncode == 0 else "fail",
        "returncode": proc.returncode,
        "cmd": " ".join(cmd),
        "output_tail": tail,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def eval_graph(graph: Path, source: Path, reindex: bool, use_advanced: bool) -> Dict[str, Any]:
    reindex_result: Optional[Dict[str, Any]] = None
    if reindex:
        cmd = [
            sys.executable, str(ROOT / "scripts" / "index_python.py"),
            "--package", str(source), "--graph", str(graph),
        ]
        if use_advanced:
            cmd.append("--use-advanced")
        reindex_result = _run(cmd, cwd=ROOT)
        if reindex_result["status"] != "ok":
            detail = reindex_result.get("reason") or "\n".join(reindex_result.get("output_tail", []))
            raise RuntimeError(f"reindex failed; refusing to evaluate a stale graph: {detail}")
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
        "semantic_suspicions": report.get("semantic_suspicion_count", 0),
        "observations": int(len(g.call_observations)),
        "clean": (
            s["anomaly_count"] == 0
            and report["dangling_count"] == 0
            and report.get("semantic_suspicion_count", 0) == 0
        ),
        "reindex": reindex_result,
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


def eval_rust(port_dir: Path, run_binary: bool = True) -> Dict[str, Any]:
    if not (port_dir / "Cargo.toml").exists():
        return {"status": "skipped", "reason": f"no Cargo.toml in {port_dir}"}
    stages: Dict[str, List[str]] = {
        "fmt": ["cargo", "fmt", "--check"],
        "check": ["cargo", "check"],
        # Run all integration tests so multi-contract ports (e.g. Version +
        # SimpleSpec) cannot pass port_eval by exercising only the first
        # golden_contract.rs file.
        "golden_test": ["cargo", "test", "--tests", "--", "--quiet"],
    }
    if run_binary:
        stages["run"] = ["cargo", "run", "--quiet"]
    results = {name: _run(cmd, cwd=port_dir) for name, cmd in stages.items()}
    results["all_ok"] = all(r.get("status") == "ok" for r in results.values())
    return results


def count_golden(source: Path) -> Dict[str, Any]:
    tests_dir = source / "tests"
    files = sorted(tests_dir.rglob("golden_*.json")) if tests_dir.exists() else []
    names = [str(p.relative_to(tests_dir)) for p in files]
    case_counts: Dict[str, int] = {}
    for p in files:
        rel_name = str(p.relative_to(tests_dir))
        data = json.loads(p.read_text())
        cases = data.get("cases")
        # mini_lang groups many golden cases per file; mini_game uses one trace
        # per file. Count the actual behavior cases when present, otherwise
        # count the file as a single golden scenario.
        case_counts[rel_name] = len(cases) if isinstance(cases, list) else 1
    return {
        "count": sum(case_counts.values()),
        "file_count": len(names),
        "names": names,
        "case_counts": case_counts,
    }


def golden_contract_coverage(
    source: Path,
    port_dir: Path,
    contract_tests: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Map source golden groups to the Rust integration tests expected to cover them."""
    tests_dir = source / "tests"
    files = sorted(tests_dir.rglob("golden_*.json")) if tests_dir.exists() else []
    groups = sorted({p.parent.relative_to(tests_dir) for p in files}, key=lambda p: str(p))
    expected: Dict[str, str] = {}
    missing: List[str] = []
    for group in groups:
        group_name = str(group)
        test_name = (contract_tests or {}).get(
            group_name,
            "golden_contract.rs" if group_name == "." else f"{'_'.join(group.parts)}_contract.rs",
        )
        expected[group_name] = test_name
        if not (port_dir / "tests" / test_name).exists():
            missing.append(test_name)
    return {
        "expected": expected,
        "missing": missing,
        "complete": not missing,
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
    contract_tests: Optional[Dict[str, str]] = None,
    run_binary: bool = True,
) -> Dict[str, Any]:
    graph_res = eval_graph(graph, source, reindex, use_advanced)
    if not symbols:
        symbols = default_key_symbols(graph)
    packs = gen_context_packs(symbols, graph, ROOT / "output" / "port_eval" / target)
    rust = (
        {"status": "skipped", "reason": "--skip-rust"}
        if skip_rust
        else eval_rust(port_dir, run_binary=run_binary)
    )
    golden = count_golden(source)
    contract_coverage = golden_contract_coverage(source, port_dir, contract_tests)

    rust_ok = rust.get("all_ok", False)
    golden_passed = (
        rust.get("golden_test", {}).get("status") == "ok" and contract_coverage["complete"]
        if not skip_rust
        else None
    )
    # Without the cargo stages we cannot assert the end-to-end (north-star) result.
    overall = None if skip_rust else bool(graph_res["clean"] and rust_ok and golden_passed)

    return {
        "target": target,
        "graph": graph_res,
        "context_packs": packs,
        "rust": rust,
        "golden_scenarios": {**golden, "passed": golden_passed, "contract_coverage": contract_coverage},
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
        f"(anomalies={g['structural_anomalies']}, dangling={g['dangling_targets']}, "
        f"semantic_suspicions={g.get('semantic_suspicions', 0)})",
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
        f"- contract coverage complete: {gs['contract_coverage']['complete']}"
        f" (missing: {', '.join(gs['contract_coverage']['missing']) or '-'})",
        "",
        f"**manual_fix_count: {r['manual_fix_count']}**",
        f"**OVERALL PASS: {r['overall_pass']}**",
    ]
    return "\n".join(lines)


def load_gate_manifest(path: Path) -> Dict[str, Dict[str, Any]]:
    """Load and fail closed on the small declarative port-gate manifest."""
    data = json.loads(path.read_text())
    entries = data.get("ports")
    if not isinstance(entries, list):
        raise ValueError(f"{path}: ports must be a list")
    gates: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"{path}: each port entry must be an object")
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident:
            raise ValueError(f"{path}: every port entry needs a non-empty id")
        if ident in gates:
            raise ValueError(f"{path}: duplicate port id {ident!r}")
        kind = entry.get("kind", "port")
        if kind not in {"port", "gap"}:
            raise ValueError(f"{path}: {ident}: kind must be port or gap")
        if kind == "port":
            for key in ("source", "port", "indexer"):
                if not isinstance(entry.get(key), str) or not entry[key]:
                    raise ValueError(f"{path}: {ident}: missing {key!r}")
            if entry["indexer"] not in {"python", "c"}:
                raise ValueError(f"{path}: {ident}: unknown indexer {entry['indexer']!r}")
        elif not isinstance(entry.get("gap"), str) or not entry["gap"]:
            raise ValueError(f"{path}: {ident}: gap entries need a named gap")
        gates[ident] = entry

    cargo_ports = {
        str(cargo.parent.relative_to(ROOT))
        for cargo in (ROOT / "examples").glob("*_rust/Cargo.toml")
    }
    declared_ports = {
        str(entry["port"])
        for entry in gates.values()
        if entry.get("kind", "port") == "port"
    }
    missing_ports = sorted(cargo_ports - declared_ports)
    stale_ports = sorted(declared_ports - cargo_ports)
    if missing_ports or stale_ports:
        raise ValueError(
            f"{path}: Rust-port coverage mismatch; missing={missing_ports or '-'} "
            f"stale={stale_ports or '-'}"
        )
    required_gaps = data.get("required_source_gaps", [])
    if not isinstance(required_gaps, list) or not all(isinstance(item, str) for item in required_gaps):
        raise ValueError(f"{path}: required_source_gaps must be a list of port ids")
    undeclared_gaps = sorted(set(required_gaps) - set(gates))
    non_gap_entries = sorted(
        ident
        for ident in required_gaps
        if ident in gates and gates[ident].get("kind", "port") != "gap"
    )
    if undeclared_gaps or non_gap_entries:
        raise ValueError(
            f"{path}: source-only coverage mismatch; missing={undeclared_gaps or '-'} "
            f"not_gap={non_gap_entries or '-'}"
        )

    # Rust ports are discovered from disk and cross-checked both ways, so one
    # cannot be dropped silently. Source packages carrying a contract need the
    # same treatment: without it a target simply vanishes from the report
    # instead of being listed with its gap. `jsonpatch` — 25 golden cases and a
    # published graph, no Rust port — was omitted exactly this way.
    contract_sources = {
        pkg.name
        for pkg in (ROOT / "examples").iterdir()
        if pkg.is_dir()
        and not pkg.name.endswith("_rust")
        and not pkg.name.startswith(".")
        and any((pkg / "tests").rglob("golden*"))
    }
    covered_sources = {
        Path(entry["source"]).name if entry.get("source") else ident
        for ident, entry in gates.items()
    }
    unlisted_sources = sorted(contract_sources - covered_sources)
    if unlisted_sources:
        raise ValueError(
            f"{path}: source package(s) with a golden contract and no profile: "
            f"{unlisted_sources}. Declare each as a port or as a named gap; "
            "silently omitting one hides a target from the evidence report."
        )

    for ident, entry in gates.items():
        after = entry.get("after", [])
        if not isinstance(after, list) or not all(isinstance(dep, str) for dep in after):
            raise ValueError(f"{path}: profile {ident!r} has invalid 'after' dependencies")
        if len(after) != len(set(after)):
            raise ValueError(f"{path}: profile {ident!r} repeats an 'after' dependency")
        missing = sorted(set(after) - set(gates))
        if missing:
            raise ValueError(
                f"{path}: profile {ident!r} depends on unknown profile(s): {missing}"
            )
        if ident in after:
            raise ValueError(f"{path}: profile {ident!r} cannot depend on itself")
    return gates


def _order_gate_ids(selected: List[str], gates: Dict[str, Dict[str, Any]]) -> List[str]:
    """Topologically order selected profiles while preserving manifest order.

    Dependencies are used only for aggregate runs.  A named ``--gate`` remains
    a focused diagnostic command, so it does not unexpectedly run neighbouring
    profiles merely because one full-suite check consumes their fresh output.
    """
    wanted = set(selected)
    ordered: List[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(ident: str) -> None:
        if ident in visited:
            return
        if ident in visiting:
            raise ValueError(f"port-gate dependency cycle includes {ident!r}")
        visiting.add(ident)
        for dep in gates[ident].get("after", []):
            if dep in wanted:
                visit(dep)
        visiting.remove(ident)
        visited.add(ident)
        ordered.append(ident)

    for ident in selected:
        visit(ident)
    return ordered


def _tool_probe(tool: str) -> Dict[str, Any]:
    """Classify a missing tool as a skip and an installed-but-broken tool as fail."""
    if tool == "c_compiler":
        for candidate in ("cc", "gcc", "clang"):
            executable = shutil.which(candidate)
            if executable is not None:
                result = _run([executable, "--version"], ROOT)
                if result["status"] == "ok":
                    return {"status": "ok", "tool": candidate}
                return {
                    "status": "fail",
                    "reason": f"C compiler {candidate!r} is present but its probe failed",
                    "detail": result,
                }
        return {"status": "skipped", "reason": "no C compiler (cc/gcc/clang) on PATH"}

    if tool == "miri":
        cargo = _tool_probe("cargo")
        if cargo["status"] != "ok":
            return cargo
        result = _run(["cargo", "+nightly", "miri", "--version"], ROOT)
        if result["status"] == "ok":
            return {"status": "ok", "tool": "cargo +nightly miri"}
        detail = "\n".join(result.get("output_tail", []))
        missing_markers = ("component 'miri'", "miri component", "not installed", "not available")
        if any(marker in detail.lower() for marker in missing_markers):
            return {"status": "skipped", "reason": "nightly Miri component is unavailable"}
        return {
            "status": "fail",
            "reason": "cargo +nightly miri is present but its probe failed",
            "detail": result,
        }

    executable = shutil.which(tool)
    if executable is None:
        return {"status": "skipped", "reason": f"{tool} not found on PATH"}
    result = _run([executable, "--version"], ROOT)
    if result["status"] == "ok":
        return {"status": "ok", "tool": tool}
    return {
        "status": "fail",
        "reason": f"{tool!r} is present but its probe failed",
        "detail": result,
    }


def _expand_gate_command(command: List[str]) -> List[str]:
    """Expand only the two stable manifest placeholders; do not invoke a shell."""
    replacements = {"{python}": sys.executable, "{root}": str(ROOT)}
    return [replacements.get(part, part) for part in command]


def _check_is_enabled(check: Dict[str, Any], full: bool, scale: bool, differential_full: bool) -> bool:
    when = check.get("when", "always")
    enabled = {
        "always": True,
        "full": full,
        "scale": scale,
        "differential-full": differential_full,
    }
    if when not in enabled:
        raise ValueError(f"unknown gate check selector {when!r}")
    return enabled[when]


def _run_declared_check(
    check: Dict[str, Any], *, full: bool, scale: bool, differential_full: bool
) -> Dict[str, Any]:
    """Execute one manifest check with explicit tool skip/fail semantics."""
    name = str(check.get("name", "unnamed check"))
    if not _check_is_enabled(check, full, scale, differential_full):
        return {"name": name, "status": "skipped", "reason": f"requires --{check['when']}"}

    for tool in check.get("tools", []):
        probe = _tool_probe(str(tool))
        if probe["status"] != "ok":
            return {"name": name, **probe}

    command = check.get("command")
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        raise ValueError(f"{name}: command must be a list of strings")
    declared_env = check.get("env", {})
    if not isinstance(declared_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in declared_env.items()
    ):
        raise ValueError(f"{name}: env must be a string-to-string object")
    declared_cwd = check.get("cwd", ".")
    if not isinstance(declared_cwd, str):
        raise ValueError(f"{name}: cwd must be a relative path string")
    cwd = (ROOT / declared_cwd).resolve()
    if ROOT not in cwd.parents and cwd != ROOT:
        raise ValueError(f"{name}: cwd must stay within the repository")
    result = _run(_expand_gate_command(command), cwd, env=declared_env)
    result["name"] = name
    successful_skip_marker = check.get("successful_skip_marker")
    if successful_skip_marker is not None:
        if not isinstance(successful_skip_marker, str) or not successful_skip_marker:
            raise ValueError(f"{name}: successful_skip_marker must be a non-empty string")
        output = "\n".join(result.get("output_tail", []))
        if result["status"] == "ok" and successful_skip_marker in output:
            result["status"] = "skipped"
            result["reason"] = str(
                check.get("skip_reason", "command passed with declared source skips")
            )
    # pytest reports a deliberately skipped optional tool with exit code zero.
    # Preserve that in the aggregate status instead of silently calling it full coverage.
    output = "\n".join(result.get("output_tail", []))
    result["reported_skip"] = bool(re.search(r"\b\d+ skipped\b", output))
    return result


def _index_gate(gate: Dict[str, Any], graph: Path) -> Dict[str, Any]:
    if gate["indexer"] == "c":
        compiler = _tool_probe("c_compiler")
        if compiler["status"] != "ok":
            return {
                "name": "c graph index",
                **compiler,
                "elapsed_seconds": 0.0,
            }
    source = ROOT / gate["source"]
    if gate["indexer"] == "python":
        command = [
            sys.executable,
            str(ROOT / "scripts" / "index_python.py"),
            "--package",
            str(source),
            "--graph",
            str(graph),
        ]
    else:
        command = [
            sys.executable,
            str(ROOT / "scripts" / "index_c.py"),
            "--package",
            str(source),
            "--graph",
            str(graph),
        ]
    result = _run(command, ROOT)
    result["name"] = f"{gate['indexer']} graph index"
    return result


def _gate_summary_line(result: Dict[str, Any]) -> str:
    graph = result.get("port_eval", {}).get("graph")
    if not graph:
        return "graph=n/a"
    return (
        f"graph={graph['structural_pass_rate']} "
        f"calls={graph['total_calls']} packs="
        f"{result['port_eval']['context_packs']['count']}/"
        f"{len(result['port_eval']['context_packs']['requested'])}"
    )


def run_declared_gate(
    gate: Dict[str, Any], *, full: bool, scale: bool, differential_full: bool
) -> Dict[str, Any]:
    """Run one complete port profile; never turn a broken present tool into a skip."""
    ident = str(gate["id"])
    if gate.get("kind", "port") == "gap":
        return {
            "id": ident,
            "status": "gap",
            "gap": gate["gap"],
            "timing": {"total_seconds": 0.0},
        }

    started = time.monotonic()
    result: Dict[str, Any] = {"id": ident, "status": "pass", "checks": []}
    for check in gate.get("checks", []):
        check_result = _run_declared_check(
            check, full=full, scale=scale, differential_full=differential_full
        )
        result["checks"].append(check_result)

    graph = ROOT / "output" / "port_gates" / ident / "graph"
    index_result = _index_gate(gate, graph)
    result["index"] = index_result

    cargo = _tool_probe("cargo")
    rustc = _tool_probe("rustc")
    result["tool_probes"] = {"cargo": cargo, "rustc": rustc}
    skip_rust = any(probe["status"] == "skipped" for probe in (cargo, rustc))
    if index_result["status"] == "ok":
        port_eval_started = time.monotonic()
        try:
            result["port_eval"] = build_eval_report(
                source=ROOT / gate["source"],
                port_dir=ROOT / gate["port"],
                graph=graph,
                target=ident,
                symbols=list(gate.get("symbols", [])),
                reindex=False,
                use_advanced=False,
                manual_fixes=int(gate.get("manual_fixes", 0)),
                skip_rust=skip_rust,
                contract_tests=dict(gate.get("contract_tests", {})),
                run_binary=bool(gate.get("run_binary", True)),
            )
        except (OSError, RuntimeError, ValueError) as error:
            result["port_eval_error"] = str(error)
        result["port_eval_seconds"] = round(time.monotonic() - port_eval_started, 3)

    failed_checks = [check for check in result["checks"] if check["status"] == "fail"]
    broken_tools = [
        probe for probe in result["tool_probes"].values() if probe["status"] == "fail"
    ]
    if index_result["status"] == "fail" or failed_checks or broken_tools or "port_eval_error" in result:
        result["status"] = "fail"
    elif (
        index_result["status"] != "skipped"
        and not skip_rust
        and result.get("port_eval", {}).get("overall_pass") is not True
    ):
        # This includes a corrupt golden: cargo test fails inside port_eval and
        # the gate must exit non-zero instead of merely printing a false report.
        result["status"] = "fail"
    else:
        skipped = index_result["status"] == "skipped" or skip_rust or any(
            check["status"] == "skipped" or check.get("reported_skip", False)
            for check in result["checks"]
        )
        result["status"] = "pass_with_skips" if skipped else "pass"

    checks_seconds = sum(float(check.get("elapsed_seconds", 0.0)) for check in result["checks"])
    index_seconds = float(index_result.get("elapsed_seconds", 0.0))
    port_eval_seconds = float(result.get("port_eval_seconds", 0.0))
    total_seconds = round(time.monotonic() - started, 3)
    result["timing"] = {
        "checks_seconds": round(checks_seconds, 3),
        "index_seconds": round(index_seconds, 3),
        "port_eval_seconds": round(port_eval_seconds, 3),
        # Tool probes and report setup are deliberately included rather than
        # disappearing from the wall-clock number.
        "other_seconds": round(max(0.0, total_seconds - checks_seconds - index_seconds - port_eval_seconds), 3),
        "total_seconds": total_seconds,
    }
    return result


def print_declared_gate_result(result: Dict[str, Any]) -> None:
    """Keep the human output compact while retaining JSON-style evidence details."""
    ident = result["id"]
    if result["status"] == "gap":
        print(f"{ident}: GAP — {result['gap']}")
        return
    print(f"== {ident} ==")
    for check in result.get("checks", []):
        suffix = (
            f" — {check.get('reason', '')}"
            if check["status"] in {"skipped", "fail"} and check.get("reason")
            else ""
        )
        print(f"  {check['status'].upper()}: {check['name']}{suffix}")
        if check["status"] == "fail":
            print("    " + "\n    ".join(check.get("output_tail", [])[-5:]))
    index = result.get("index", {})
    print(f"  {index.get('status', 'n/a').upper()}: {index.get('name', 'graph index')}")
    for tool, probe in result.get("tool_probes", {}).items():
        if probe["status"] != "ok":
            print(f"  {probe['status'].upper()}: {tool} — {probe.get('reason', '')}")
    if "port_eval" in result:
        port = result["port_eval"]
        print(
            f"  PORT_EVAL: { _gate_summary_line(result) }; "
            f"golden={port['golden_scenarios']['count']} "
            f"manual_fixes={port['manual_fix_count']} overall={port['overall_pass']}"
        )
    if "port_eval_error" in result:
        print(f"  FAIL: port_eval setup — {result['port_eval_error']}")
    timing = result.get("timing")
    if timing:
        print(
            "  TIMING: "
            f"checks={timing.get('checks_seconds', 0.0):.3f}s "
            f"index={timing.get('index_seconds', 0.0):.3f}s "
            f"port_eval={timing.get('port_eval_seconds', 0.0):.3f}s "
            f"other={timing.get('other_seconds', 0.0):.3f}s "
            f"total={timing.get('total_seconds', 0.0):.3f}s"
        )
    print(f"  RESULT: {result['status'].upper()}")


def main(
    graph: Optional[Path] = typer.Option(None, "--graph", help="BYOG graph root (e.g. byog_mini_game)"),
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
    gate: List[str] = typer.Option(
        [], "--gate", help="Run a named manifest-backed evidence gate (repeatable)."
    ),
    all_gates: bool = typer.Option(
        False, "--all-gates", help="Run every manifest-backed port or declared gap."
    ),
    gate_manifest: Path = typer.Option(
        DEFAULT_GATE_MANIFEST,
        "--gate-manifest",
        help="Declarative port-evidence manifest used by --gate/--all-gates.",
    ),
    full: bool = typer.Option(
        False, "--full", help="Enable manifest checks marked full (for example Miri/full pytest)."
    ),
    scale: bool = typer.Option(
        False, "--scale", help="Enable manifest checks marked scale (charset compatibility mode)."
    ),
    differential_full: bool = typer.Option(
        False,
        "--differential-full",
        help="Enable manifest checks marked differential-full (charset compatibility mode).",
    ),
) -> None:
    """Run the Python->Rust port eval and emit a repeatable report."""
    if gate or all_gates:
        if graph is not None:
            raise typer.BadParameter("--graph cannot be combined with --gate/--all-gates")
        try:
            gates = load_gate_manifest(
                gate_manifest if gate_manifest.is_absolute() else ROOT / gate_manifest
            )
        except (OSError, ValueError, json.JSONDecodeError) as error:
            typer.secho(f"port-gate manifest error: {error}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2) from error
        selected = list(gates) if all_gates else gate
        unknown = [ident for ident in selected if ident not in gates]
        if unknown:
            typer.secho(f"unknown port gate(s): {', '.join(unknown)}", fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        if not selected:
            typer.secho("no port gates selected", fg=typer.colors.RED, err=True)
            raise typer.Exit(2)
        if all_gates:
            try:
                selected = _order_gate_ids(selected, gates)
            except ValueError as error:
                typer.secho(f"port-gate manifest error: {error}", fg=typer.colors.RED, err=True)
                raise typer.Exit(2) from error
        aggregate_started = time.monotonic()
        reports = [
            run_declared_gate(
                gates[ident], full=full, scale=scale, differential_full=differential_full
            )
            for ident in selected
        ]
        for report in reports:
            print_declared_gate_result(report)
        aggregate_seconds = time.monotonic() - aggregate_started
        timed_profiles = [
            f"{report['id']}={report['timing']['total_seconds']:.3f}s"
            for report in reports
            if report["status"] != "gap" and "timing" in report
        ]
        if timed_profiles:
            print(
                "PORT EVIDENCE TIMING: "
                f"wall={aggregate_seconds:.3f}s; profiles=" + ", ".join(timed_profiles)
            )
        failed = [report["id"] for report in reports if report["status"] == "fail"]
        skipped = [report["id"] for report in reports if report["status"] == "pass_with_skips"]
        gaps = [report["id"] for report in reports if report["status"] == "gap"]
        if failed:
            print(f"PORT EVIDENCE: FAIL ({', '.join(failed)})")
            raise typer.Exit(1)
        if skipped:
            suffix = f"; DECLARED GAPS ({', '.join(gaps)})" if gaps else ""
            print(f"PORT EVIDENCE: PASS WITH SKIPS ({', '.join(skipped)}){suffix}")
        elif gaps:
            print(f"PORT EVIDENCE: PASS WITH DECLARED GAPS ({', '.join(gaps)})")
        else:
            print("PORT EVIDENCE: PASS")
        return

    if graph is None:
        raise typer.BadParameter("--graph is required unless --gate or --all-gates is used")
    report = build_eval_report(
        source=source if source.is_absolute() else ROOT / source,
        port_dir=port if port.is_absolute() else ROOT / port,
        graph=graph if graph.is_absolute() else ROOT / graph,
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
          f"semantic_suspicions={g.get('semantic_suspicions', 0)}, "
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
    coverage = gs["contract_coverage"]
    if not coverage["complete"]:
        print(f"golden coverage   : missing {coverage['missing']}")
    print(f"manual fixes      : {report['manual_fix_count']}")
    print(f"OVERALL PASS      : {report['overall_pass']}")
    if markdown is not None:
        print(f"(markdown written : {markdown})")


if __name__ == "__main__":
    typer.run(main)
