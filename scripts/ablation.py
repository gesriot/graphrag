#!/usr/bin/env python
"""Ablation harness: does the deterministic graph + context packs measurably help
a cold porting agent versus raw source alone?

Two arms per target, each a self-contained Cargo *kit* a fresh subagent fills in:
  - arm_graph: only the graph-derived context pack(s) + the API spec + a prompt.
  - arm_raw:   only the raw original source      + the API spec + a prompt.

Neither kit contains the golden corpus or the reference Rust port. After the
subagents finish, `eval` scores each kit against the *hidden* golden by injecting
the reference contract test (crate name + golden path patched) into a throwaway
copy and running cargo build/test.

This is an honest engineering ablation, not a cryptographically sealed lab: the
kits share a filesystem, so the prompt forbids reading outside the kit and the
subagent transcript is the audit trail.

Subcommands:
  prep  --target T --graph G --source F... --symbol S... --api A --out DIR
  eval  --kit DIR --golden-dir D --contract-test F --crate-name N
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import typer

ROOT = Path(__file__).resolve().parents[1]
app = typer.Typer(add_completion=False)


PROMPT_TEMPLATE = """\
# Port task ({arm} arm)

You are porting a bounded source component/API to Rust. Work **only inside this kit
directory** (the directory containing this PROMPT.md). Do not read, list, or open
any file outside it — that is part of the experiment's rules.

## Your source of truth
{material}

## Required public API (implement exactly this, in `src/lib.rs` and any modules)
{api}

## Available dependencies
{deps_note}

## Rules
- Derive the behaviour from your source of truth above. Do not search the web or
  rely on a remembered version of this library; port what you are given.
- Put the implementation in `src/lib.rs` (plus modules if you like). Keep
  `src/main.rs` as-is (it is a thin CLI; you may leave it).
- It must build: run `cargo build` and `cargo test --no-run` until they succeed.
- Do NOT write any tests yourself, and there is no reference output to match —
  just implement the API faithfully from your source of truth.

## When done, report
- how many compile attempts (cargo build invocations) it took to build clean,
- anything you were unsure about given only your source of truth.
"""

CARGO_TOML = """\
[package]
name = "arm"
version = "0.1.0"
edition = "2021"

[dependencies]
"""

LIB_STUB = """\
// Implement the required public API here (see PROMPT.md).
"""

MAIN_STUB = """\
fn main() {
    // Thin CLI placeholder; the ablation scores the library API, not this.
}
"""


def reachable_functions(graph: Path, roots: list[str]) -> list[str]:
    """Transitive closure of `roots` over `calls` edges (functions/methods only).

    A hand-picked symbol list under-packs the graph arm (the jsmn dry-run had to
    infer a callee that was never packed); the closure gives the arm exactly the
    reachable subgraph the raw arm would have to assemble itself.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from byog_graph import ByogGraph  # type: ignore
    from collections import defaultdict, deque

    g = ByogGraph(graph.resolve())
    calls = g.rels[g.rels["type"].astype(str) == "calls"]
    adj: dict[str, list[str]] = defaultdict(list)
    for s, t in zip(calls["source"].astype(str), calls["target"].astype(str)):
        adj[s].append(t)
    titles = set(g.ents["title"].astype(str))
    seen: set[str] = set()
    q = deque(roots)
    while q:
        n = q.popleft()
        if n in seen:
            continue
        seen.add(n)
        for m in adj.get(n, []):
            if m not in seen:
                q.append(m)
    # keep only real entities (drop dangling observation-only targets)
    return sorted(t for t in seen if t in titles)


@app.command()
def prep(
    target: str = typer.Option(...),
    graph: Path = typer.Option(...),
    source: list[Path] = typer.Option(..., "--source", help="raw source file(s)/dir for arm_raw"),
    symbol: list[str] = typer.Option([], "--symbol", help="explicit graph symbols to pack"),
    closure_root: list[str] = typer.Option([], "--closure-root", help="roots; pack their transitive callee closure"),
    dep: list[str] = typer.Option([], "--dep", help="Cargo dep line(s) pre-provided to BOTH kits, e.g. 'fancy-regex = \"0.13\"'"),
    api: Path = typer.Option(..., "--api", help="markdown file with the required API spec"),
    out: Path = typer.Option(..., "--out", help="output dir; arm_graph/ and arm_raw/ created"),
):
    """Build the two isolated kits for a target."""
    out = out.resolve()
    api_text = api.read_text()
    pack_script = ROOT / "scripts" / "context_pack.py"
    symbols = list(symbol)
    if closure_root:
        symbols = sorted(set(symbols) | set(reachable_functions(graph, list(closure_root))))
    cargo_toml = CARGO_TOML + ("\n".join(dep) + "\n" if dep else "")
    deps_note = (
        "These crates are already in `Cargo.toml` (available offline): "
        + ", ".join(dep) + ". Use them rather than hand-rolling equivalents."
        if dep else "Standard library only; no extra crates are provided."
    )

    def write_kit(arm: str, material_dir_setup):
        kit = out / arm
        if kit.exists():
            shutil.rmtree(kit)
        (kit / "src").mkdir(parents=True)
        (kit / "Cargo.toml").write_text(cargo_toml)
        (kit / "src" / "lib.rs").write_text(LIB_STUB)
        (kit / "src" / "main.rs").write_text(MAIN_STUB)
        material = material_dir_setup(kit)
        (kit / "PROMPT.md").write_text(
            PROMPT_TEMPLATE.format(arm=arm, kit=kit, material=material, api=api_text, deps_note=deps_note)
        )
        return kit

    def graph_material(kit: Path) -> str:
        ctx = kit / "context"
        ctx.mkdir()
        made = []
        for sym in symbols:
            out_file = ctx / f"pack_{sym.replace(':', '_')}.json"
            res = subprocess.run(
                [sys.executable, str(pack_script), sym, "--graph", str(graph.resolve()),
                 "--purpose", "port-to-rust", "--max-text-chars", "0", "--output", str(out_file)],
                cwd=ROOT, capture_output=True, text=True,
            )
            if res.returncode == 0 and out_file.exists():
                made.append(out_file.name)
        return (
            "Graph-derived **context packs** in `context/` (" + ", ".join(made) + "). "
            "Each pack lists the relevant entities, their call edges, code snippets, "
            "and weak observations (external/undefined calls). This is the only "
            "material you get; there is no raw source file in this kit."
        )

    def raw_material(kit: Path) -> str:
        srcdir = kit / "src_orig"
        srcdir.mkdir()
        names = []
        for f in source:
            if f.is_dir():
                shutil.copytree(f, srcdir / f.name, ignore=shutil.ignore_patterns(
                    "tests", "target", "__pycache__", "*.pyc", ".git"))
                names.append(f.name + "/ (whole package, tests excluded)")
            else:
                shutil.copy(f, srcdir / f.name)
                names.append(f.name)
        return (
            "Raw original source in `src_orig/` (" + ", ".join(names) + "). This is "
            "the complete original; locate and port the relevant function(s) from "
            "it. There is no graph or context pack in this kit."
        )

    kg = write_kit("arm_graph", graph_material)
    kr = write_kit("arm_raw", raw_material)
    # leak check: no golden / reference port anywhere in the kits
    leaks = []
    for kit in (kg, kr):
        for p in kit.rglob("*"):
            if p.is_file() and ("golden" in p.name or "parse_contract" in p.name):
                leaks.append(str(p))
    manifest = {"target": target, "arm_graph": str(kg), "arm_raw": str(kr), "leaks": leaks}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    if leaks:
        raise SystemExit("LEAK: golden/reference found in a kit")


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


@app.command()
def eval(
    kit: Path = typer.Option(...),
    golden_dir: Path = typer.Option(..., help="dir holding golden_*.json (reference)"),
    contract_test: Path = typer.Option(..., help="reference tests/parse_contract.rs"),
    crate_name: str = typer.Option(..., help="crate name used in the reference test (e.g. jsmn_rust)"),
):
    """Score a filled-in kit against the hidden golden in a throwaway copy."""
    kit = kit.resolve()
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "eval"
        shutil.copytree(kit, work)
        # inject the reference contract test, patched to this crate + abs golden path
        test_src = contract_test.read_text()
        test_src = test_src.replace(crate_name, "arm")
        # repoint the golden-dir lookup (any `fn *golden*() -> PathBuf {..}`) to abs
        patched = re.sub(
            r"fn (\w*golden\w*)\(\) -> PathBuf \{.*?\n\}",
            lambda mm: 'fn %s() -> PathBuf {\n    PathBuf::from(r"%s")\n}'
            % (mm.group(1), str(golden_dir.resolve())),
            test_src,
            flags=re.S,
        )
        (work / "tests").mkdir(exist_ok=True)
        (work / "tests" / "parse_contract.rs").write_text(patched)
        # the contract test needs serde dev-deps
        cargo = (work / "Cargo.toml").read_text()
        if "[dev-dependencies]" not in cargo:
            cargo += '\n[dev-dependencies]\nserde = { version = "1.0", features = ["derive"] }\nserde_json = "1.0"\n'
            (work / "Cargo.toml").write_text(cargo)

        build = _run(["cargo", "build"], work)
        test = _run(["cargo", "test", "--test", "parse_contract", "--", "--quiet"], work)
        passed = test.returncode == 0
        m = re.search(r"(\d+) passed", test.stdout)
        result = {
            "kit": str(kit),
            "builds": build.returncode == 0,
            "golden_pass": passed,
            "tests_passed": int(m.group(1)) if m else 0,
            "build_tail": build.stderr.strip().splitlines()[-3:] if build.returncode else [],
            "test_tail": (test.stdout + test.stderr).strip().splitlines()[-4:] if not passed else [],
        }
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    app()
