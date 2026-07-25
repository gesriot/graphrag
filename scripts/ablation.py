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
  adequacy --graph G --spec S
  prep  --target T --graph G --source F... --symbol S... --dep D... --api A --out DIR
  audit --out DIR --graph G --spec S
  eval  --kit DIR --golden-dir D --contract-test F --crate-name N
        [--record DIR --arm A --run N --target T]
        [--self-build-attempts N --self-tool-uses N --self-wall-s S]
  report --runs DIR
"""
from __future__ import annotations

import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
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


# Edge types the apply-slice closure follows. NOTE: `contains` is deliberately
# NOT followed -- class-expansion (pull every member of a reached class) overpacks
# (reaching JsonPatch would drag in its diff-side from_diff/to_string ->
# DiffBuilder). The closure must reach *specific methods* via precise edges, which
# forces the resolver to emit them (constructor/inheritance/property/dispatch).
CLOSURE_EDGES = ("calls", "uses_data", "references", "imports", "inherits", "property", "dispatch")


def closure(graph: Path, roots: list[str], follow=CLOSURE_EDGES) -> set[str]:
    """Reachable entity set from `roots`, following only the given edge types.

    Edge types that do not exist yet are simply empty, so the same measurement
    tracks adequacy as resolver edges are added step by step. No blanket class
    member expansion -- that overpacks; specific members must be reached by edges.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from byog_graph import ByogGraph  # type: ignore
    from collections import defaultdict, deque

    g = ByogGraph(graph.resolve())
    rels = g.rels
    etype = rels["type"].astype(str)
    src = rels["source"].astype(str)
    tgt = rels["target"].astype(str)

    fwd: dict[str, list[str]] = defaultdict(list)
    for s, t, e in zip(src, tgt, etype):
        if e in follow:
            fwd[s].append(t)

    titles = set(g.ents["title"].astype(str))
    seen: set[str] = set()
    q = deque(roots)
    while q:
        n = q.popleft()
        if n in seen:
            continue
        seen.add(n)
        for m in fwd.get(n, []):
            q.append(m)
    return {t for t in seen if t in titles}


def _strip_kit_paths(text: str) -> str:
    """Reduce absolute in-repo source paths to bare file names.

    Provenance in a kit should say *which module* a snippet came from, not where
    the original tree is on disk.
    """
    return re.sub(re.escape(str(ROOT)) + r"[\w./-]*/([\w.-]+)", r"\1", text)


def packable_symbols(graph: Path, symbols: list[str]) -> list[str]:
    """Symbols safe to materialize as context packs for an ablation kit.

    The adequacy closure deliberately does not follow `contains`, but a generic
    class/module/file pack can still leak broad source spans through the entity's
    own text unit. For ablation kits, pack only narrow behavioral/data entities;
    precise resolver edges should reach individual methods/data, not smuggle an
    entire class body into the graph arm.
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    from byog_graph import ByogGraph  # type: ignore

    g = ByogGraph(graph.resolve())
    by_title = {
        str(row["title"]): str(row.get("type", "")).lower()
        for _, row in g.ents.iterrows()
    }
    broad = {"class", "module", "file"}
    return [s for s in symbols if by_title.get(s, "") not in broad]


@app.command()
def adequacy(
    graph: Path = typer.Option(...),
    spec: Path = typer.Option(None, "--spec", help="JSON with roots/must_reach/must_exclude"),
    root: list[str] = typer.Option([], "--root"),
    must_reach: list[str] = typer.Option([], "--must-reach"),
    must_exclude: list[str] = typer.Option([], "--must-exclude"),
):
    """Measure closure adequacy: must-reach present, must-exclude leaked, size."""
    if spec is not None:
        s = json.loads(spec.read_text())
        root = list(root) + s.get("roots", [])
        must_reach = list(must_reach) + s.get("must_reach", [])
        must_exclude = list(must_exclude) + s.get("must_exclude", [])
    reached = closure(graph, list(root))
    missing = sorted(m for m in must_reach if m not in reached)
    leaked = sorted(x for x in must_exclude if x in reached)
    report = {
        "roots": list(root),
        "closure_size": len(reached),
        "must_reach_total": len(must_reach),
        "must_reach_missing": missing,
        "must_exclude_leaked": leaked,
        "adequate": not missing and not leaked,
    }
    print(json.dumps(report, indent=2))


@app.command()
def prep(
    target: str = typer.Option(...),
    graph: Path = typer.Option(...),
    source: list[Path] = typer.Option(..., "--source", help="raw source file(s)/dir for arm_raw"),
    symbol: list[str] = typer.Option([], "--symbol", help="explicit graph symbols to pack"),
    closure_root: list[str] = typer.Option([], "--closure-root", help="roots; pack their transitive graph closure"),
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
        symbols = sorted(set(symbols) | closure(graph, list(closure_root)))
    packed_symbols = packable_symbols(graph, symbols)
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
        packed_set = set(packed_symbols)
        made = []
        for sym in packed_symbols:
            out_file = ctx / f"pack_{sym.replace(':', '_')}.json"
            res = subprocess.run(
                [sys.executable, str(pack_script), sym, "--graph", str(graph.resolve()),
                 "--purpose", "port-to-rust", "--max-text-chars", "0", "--no-neighbor-text",
                 "--output", str(out_file)],
                cwd=ROOT, capture_output=True, text=True,
            )
            if res.returncode == 0 and out_file.exists():
                # Scope edge lists to the packed closure: a shared helper's full
                # neighbor list would otherwise leak the existence of out-of-scope
                # callers (e.g. i18n:_gettext is called by time/filesize), giving
                # the graph arm structure the closure deliberately excludes.
                pack = json.loads(out_file.read_text())
                for key in ("neighbors", "data_dependency_edges"):
                    edges = pack.get(key)
                    if isinstance(edges, list):
                        pack[key] = [
                            e for e in edges
                            if str(e.get("source", "")) in packed_set
                            and str(e.get("target", "")) in packed_set
                        ]
                deps = pack.get("data_dependencies")
                if isinstance(deps, list):
                    pack["data_dependencies"] = [
                        d for d in deps
                        if str(d.get("title", "")) in packed_set
                    ]
                # The generic pack is written for in-repo use, where pointing at
                # the original file is the point. Inside a kit it is the opposite:
                # absolute source paths tell the graph arm exactly where the raw
                # source lives, and `usage_hint` explicitly invites reading it,
                # both of which contradict the kit-isolation rule in PROMPT.md.
                # Keep the module identity (basename), drop the filesystem path.
                pack.pop("usage_hint", None)
                out_file.write_text(_strip_kit_paths(
                    json.dumps(pack, indent=2, ensure_ascii=False)))
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
                # PROVENANCE.md is *our* vendoring/experiment note, not upstream
                # source: it names the slice under test and its known nuances, so
                # copying it would hand arm_raw the answer key.
                shutil.copytree(f, srcdir / f.name, ignore=shutil.ignore_patterns(
                    "tests", "target", "__pycache__", "*.pyc", ".git", "PROVENANCE.md"))
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
    leaks = _scan_kit_leaks(kg) + _scan_kit_leaks(kr)
    manifest = {
        "target": target,
        "arm_graph": str(kg),
        "arm_raw": str(kr),
        "symbols": symbols,
        "packed_symbols": packed_symbols,
        "omitted_broad_symbols": sorted(set(symbols) - set(packed_symbols)),
        "closure_roots": list(closure_root),
        "deps": list(dep),
        "sources": [str(s) for s in source],
        "leaks": leaks,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    if leaks:
        raise SystemExit("LEAK: golden/reference found in a kit")


def _scan_kit_leaks(kit: Path) -> list[str]:
    """Leak check shared by prep and audit.

    Names catch the golden/reference port; the content scan catches the two
    subtler leaks: our own provenance/experiment notes vendored next to the
    source, and absolute paths pointing back out of the kit.
    """
    leaks: list[str] = []
    for p in kit.rglob("*"):
        if not p.is_file():
            continue
        if "golden" in p.name or "parse_contract" in p.name or p.name == "PROVENANCE.md":
            leaks.append(str(p))
            continue
        if p.name == "PROMPT.md":
            continue  # states the arm/protocol by design, identically per arm
        try:
            if str(ROOT) in p.read_text():
                leaks.append(f"{p} (absolute repo path)")
        except (UnicodeDecodeError, OSError):
            pass
    return leaks


def _prompt_api_section(prompt: str) -> str:
    """API-spec body between the Required public API and Available dependencies headings."""
    lines = prompt.splitlines(keepends=True)
    start = end = None
    for i, line in enumerate(lines):
        if start is None and line.startswith("## Required public API"):
            start = i + 1
            continue
        if start is not None and line.startswith("## Available dependencies"):
            end = i
            break
    if start is None:
        return ""
    return "".join(lines[start:end] if end is not None else lines[start:])


def _cargo_dep_block(cargo_toml: str) -> str:
    """Text of the [dependencies] table (excluding dev-dependencies)."""
    m = re.search(r"^\[dependencies\]\s*\n(.*?)(?=^\[|\Z)", cargo_toml, flags=re.S | re.M)
    return (m.group(1) if m else "").strip()


def _pack_filename(symbol: str) -> str:
    return f"pack_{symbol.replace(':', '_')}.json"


# One-line summary printed by per-case contract tests (see duration_contract.rs).
ABLATION_SCORE_RE = re.compile(r"^ABLATION_SCORE\s+(\{.*\})\s*$", re.M)


def _parse_ablation_score(output: str) -> dict | None:
    """Parse `ABLATION_SCORE {...}` from cargo test stdout; None if absent/malformed."""
    m = ABLATION_SCORE_RE.search(output)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


@app.command()
def audit(
    out: Path = typer.Option(..., "--out", help="prepped output dir (prep --out)"),
    graph: Path = typer.Option(..., "--graph"),
    spec: Path = typer.Option(..., "--spec", help="adequacy JSON with roots/must_reach/must_exclude"),
):
    """Dry-prep material audit against an already-prepped output dir.

    Checks: must-reach packs present; packed == closure minus broad spans;
    no must-exclude in the graph kit; kit leak scan (same as prep); both arms
    carry byte-identical API-spec text and identical Cargo dependency lists.
    Prints a JSON report; exits non-zero on any failed check.
    """
    out = out.resolve()
    adeq = json.loads(spec.read_text())
    roots = list(adeq.get("roots", []))
    must_reach = list(adeq.get("must_reach", []))
    must_exclude = list(adeq.get("must_exclude", []))

    arm_graph = out / "arm_graph"
    arm_raw = out / "arm_raw"
    checks: dict = {}
    overall = True

    # --- packed == closure minus broad spans; must-reach packs present ---
    reached = closure(graph, roots)
    expected_packed = packable_symbols(graph, sorted(reached))
    expected_packed_set = set(expected_packed)
    omitted_broad = sorted(reached - expected_packed_set)

    ctx = arm_graph / "context"
    actual_pack_files = sorted(p.name for p in ctx.glob("pack_*.json")) if ctx.is_dir() else []
    expected_pack_names = sorted(_pack_filename(sym) for sym in expected_packed)
    missing_packs = sorted(set(expected_pack_names) - set(actual_pack_files))
    extra_packs = sorted(set(actual_pack_files) - set(expected_pack_names))
    packed_eq = not missing_packs and not extra_packs

    # Every must_reach is either a pack on disk or an intentional broad omission.
    must_reach_missing: list[str] = []
    for m in must_reach:
        if _pack_filename(m) in actual_pack_files:
            continue
        if m in omitted_broad:
            continue
        must_reach_missing.append(m)
    must_reach_ok = not must_reach_missing

    checks["must_reach_packs"] = {
        "pass": must_reach_ok,
        "missing": must_reach_missing,
        "must_reach_total": len(must_reach),
    }
    checks["packed_equals_closure_minus_broad"] = {
        "pass": packed_eq,
        "closure_size": len(reached),
        "expected_packed": sorted(expected_packed_set),
        "actual_pack_files": actual_pack_files,
        "missing_packs": missing_packs,
        "extra_packs": extra_packs,
        "omitted_broad_spans": omitted_broad,
    }
    overall = overall and must_reach_ok and packed_eq

    # --- no must_exclude symbol appears anywhere in the graph kit ---
    exclude_hits: list[dict] = []
    if arm_graph.is_dir():
        for p in arm_graph.rglob("*"):
            if not p.is_file():
                continue
            try:
                text = p.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            for ex in must_exclude:
                if ex in text:
                    exclude_hits.append({"symbol": ex, "file": str(p.relative_to(arm_graph))})
    checks["must_exclude_absent"] = {
        "pass": not exclude_hits,
        "hits": exclude_hits,
    }
    overall = overall and not exclude_hits

    # --- kit leaks (reuse prep's scanner) ---
    leaks: list[str] = []
    if arm_graph.is_dir():
        leaks.extend(_scan_kit_leaks(arm_graph))
    else:
        leaks.append(f"missing kit: {arm_graph}")
    if arm_raw.is_dir():
        leaks.extend(_scan_kit_leaks(arm_raw))
    else:
        leaks.append(f"missing kit: {arm_raw}")
    checks["kit_leaks"] = {"pass": not leaks, "leaks": leaks}
    overall = overall and not leaks

    # --- byte-identical API-spec text; identical dependency lists ---
    api_graph = api_raw = None
    if (arm_graph / "PROMPT.md").is_file() and (arm_raw / "PROMPT.md").is_file():
        api_graph = _prompt_api_section((arm_graph / "PROMPT.md").read_text())
        api_raw = _prompt_api_section((arm_raw / "PROMPT.md").read_text())
    api_ok = api_graph is not None and api_raw is not None and api_graph == api_raw and api_graph != ""
    checks["api_spec_identical"] = {
        "pass": bool(api_ok),
        "graph_len": len(api_graph or ""),
        "raw_len": len(api_raw or ""),
    }
    overall = overall and bool(api_ok)

    deps_graph = deps_raw = None
    if (arm_graph / "Cargo.toml").is_file() and (arm_raw / "Cargo.toml").is_file():
        deps_graph = _cargo_dep_block((arm_graph / "Cargo.toml").read_text())
        deps_raw = _cargo_dep_block((arm_raw / "Cargo.toml").read_text())
    deps_ok = deps_graph is not None and deps_raw is not None and deps_graph == deps_raw
    checks["deps_identical"] = {
        "pass": bool(deps_ok),
        "graph_deps": deps_graph,
        "raw_deps": deps_raw,
    }
    overall = overall and bool(deps_ok)

    report = {
        "out": str(out),
        "graph": str(graph),
        "spec": str(spec),
        "roots": roots,
        "checks": checks,
        "pass": overall,
    }
    print(json.dumps(report, indent=2))
    if not overall:
        raise SystemExit(1)


def _eval_kit(
    kit: Path,
    golden_dir: Path,
    contract_test: Path,
    crate_name: str,
) -> dict:
    """Score a filled-in kit; returns the eval result dict (also printed by `eval`)."""
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
        # --nocapture so ABLATION_SCORE lines from per-case contract tests reach us.
        test = _run(
            ["cargo", "test", "--test", "parse_contract", "--", "--nocapture"],
            work,
        )
        passed = test.returncode == 0
        combined = test.stdout + test.stderr
        m = re.search(r"(\d+) passed", test.stdout) or re.search(r"(\d+) passed", combined)
        result = {
            "kit": str(kit),
            "builds": build.returncode == 0,
            "golden_pass": passed,
            "tests_passed": int(m.group(1)) if m else 0,
            "build_tail": build.stderr.strip().splitlines()[-3:] if build.returncode else [],
            "test_tail": combined.strip().splitlines()[-4:] if not passed else [],
        }
        score = _parse_ablation_score(combined)
        if score is not None:
            result["cases_passed"] = int(score.get("passed", 0))
            result["cases_total"] = int(score.get("total", 0))
            failed = score.get("failed", [])
            result["cases_failed"] = list(failed) if isinstance(failed, list) else []
        elif build.returncode != 0 or not passed:
            # Build failed, or panic/abort before the summary line — sane zero score.
            result["cases_passed"] = 0
            result["cases_total"] = 0
            result["cases_failed"] = []
        # else: older contract test that fully passed — binary fields only.
        return result


def _run_artifact_path(record_dir: Path, arm: str, run: int) -> Path:
    # Flat, stable name: re-running the same arm/run overwrites deliberately.
    safe_arm = re.sub(r"[^\w.-]+", "_", arm)
    return record_dir / f"run_{safe_arm}_{run}.json"


@app.command("eval")
def eval_cmd(
    kit: Path = typer.Option(...),
    golden_dir: Path = typer.Option(..., help="dir holding golden_*.json (reference)"),
    contract_test: Path = typer.Option(..., help="reference tests/parse_contract.rs"),
    crate_name: str = typer.Option(..., help="crate name used in the reference test (e.g. jsmn_rust)"),
    record: Path | None = typer.Option(
        None, "--record", help="per-target run directory; write run_<arm>_<n>.json here"
    ),
    arm: str | None = typer.Option(None, "--arm", help="arm label for the artifact (arm_graph|arm_raw)"),
    run: int | None = typer.Option(None, "--run", help="1-based run index within the arm"),
    target: str | None = typer.Option(None, "--target", help="target label stored in the artifact"),
    self_build_attempts: int | None = typer.Option(
        None, "--self-build-attempts", help="agent self-reported cargo build attempts"
    ),
    self_tool_uses: int | None = typer.Option(
        None, "--self-tool-uses", help="agent self-reported tool-use count"
    ),
    self_wall_s: float | None = typer.Option(
        None, "--self-wall-s", help="agent self-reported wall time in seconds"
    ),
):
    """Score a filled-in kit against the hidden golden in a throwaway copy.

    With --record, also persists a run artifact under DIR (objective eval fields
    plus optional self-reported efficiency numbers, clearly keyed as such).
    """
    # --target is required so the archive is self-describing: `report` refuses to
    # blend runs from different targets, which it can only do if each run says so.
    if record is not None and (arm is None or run is None or target is None):
        raise SystemExit("eval --record requires --arm, --run and --target")
    if record is None and (arm is not None or run is not None or target is not None):
        raise SystemExit("--arm/--run/--target only apply with --record")
    if any(v is not None for v in (self_build_attempts, self_tool_uses, self_wall_s)) and record is None:
        raise SystemExit("self-reported flags require --record (they only live on the artifact)")

    result = _eval_kit(kit, golden_dir, contract_test, crate_name)
    print(json.dumps(result, indent=2))

    if record is None:
        return

    record = record.resolve()
    record.mkdir(parents=True, exist_ok=True)
    # Self-reported numbers are never mixed into the objective eval block.
    self_reported = {
        "build_attempts": self_build_attempts,
        "tool_uses": self_tool_uses,
        "wall_s": self_wall_s,
    }
    artifact = {
        "target": target,
        "arm": arm,
        "run": run,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provenance": {
            "kit": str(kit.resolve()),
            "golden_dir": str(golden_dir.resolve()),
            "contract_test": str(contract_test.resolve()),
            "crate_name": crate_name,
        },
        "eval": result,
        "self_reported": self_reported,
    }
    path = _run_artifact_path(record, arm, run)
    path.write_text(json.dumps(artifact, indent=2) + "\n")
    # Point at the artifact without burying the eval JSON (already printed).
    print(json.dumps({"recorded": str(path)}, indent=2), file=sys.stderr)


def _median(values: list[float]) -> float:
    """Median with an explicit even-n convention: average of the two middle values.

    N=3 (the usual protocol) uses the middle element of the sorted list. Even n is
    supported for incomplete / exploratory aggregates and is stated in the report.
    """
    if not values:
        raise ValueError("median of empty list")
    return float(statistics.median(values))


def _fmt_num(x: float | int) -> str:
    """Format a score/median: integers as ints, even-n half-medians as one decimal."""
    if isinstance(x, float) and not math.isnan(x) and abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    if isinstance(x, float):
        return f"{x:.1f}"
    return str(x)


def _score_display(passed: int | float, total: int | None) -> str:
    if total is None or total == 0:
        # No per-case total (legacy binary, or unbuildable without score line).
        return _fmt_num(passed)
    return f"{_fmt_num(passed)}/{int(total)}"


def _range_with_med(values: list[float]) -> str:
    """`min–max (med M)` when n>1 and spread; single value when constant/n==1; — if empty."""
    present = [v for v in values if v is not None]
    if not present:
        return "—"
    lo, hi = min(present), max(present)
    if len(present) == 1 or lo == hi:
        return _fmt_num(lo)
    med = _median(present)
    return f"{_fmt_num(lo)}–{_fmt_num(hi)} (med {_fmt_num(med)})"


def _load_run_artifacts(runs_dir: Path) -> list[dict]:
    artifacts = []
    for p in sorted(runs_dir.glob("run_*.json")):
        try:
            data = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise SystemExit(f"report: cannot read {p}: {e}") from e
        if not isinstance(data, dict) or "arm" not in data or "run" not in data:
            raise SystemExit(f"report: {p} is not a run artifact (need arm + run)")
        data["_path"] = str(p)
        artifacts.append(data)
    return artifacts


@app.command()
def report(
    runs: Path = typer.Option(..., "--runs", help="per-target run directory written by eval --record"),
    allow_unbalanced: bool = typer.Option(
        False,
        "--allow-unbalanced",
        help="emit a table even when arms have different run counts (still labelled)",
    ),
):
    """Aggregate recorded runs into the PHASE7_ABLATION.md N=3 table shape + JSON.

    Objective scores come from eval; build attempts / tool-uses / wall are
    self-reported and labelled as such. A non-building run is scored 0, not
    dropped. Refuses a silent lopsided table when arm run counts differ.
    """
    runs = runs.resolve()
    if not runs.is_dir():
        raise SystemExit(f"report: not a directory: {runs}")

    artifacts = _load_run_artifacts(runs)
    if not artifacts:
        raise SystemExit(f"report: no run_*.json artifacts in {runs}")

    # Runs from different targets or against differently-sized goldens are not
    # comparable, and blending them fabricates the table: a stale artifact left
    # in the directory would supply a denominator no arm was ever scored against.
    # A results archive must refuse that rather than average across it.
    seen_targets = {str(a.get("target")) for a in artifacts}
    if len(seen_targets) > 1:
        listing = ", ".join(
            f"{Path(a['_path']).name}: {a.get('target')}" for a in artifacts
        )
        raise SystemExit(
            f"report: run artifacts disagree on target ({sorted(seen_targets)}); "
            f"refusing to blend incomparable runs -- {listing}"
        )

    by_arm: dict[str, list[dict]] = {}
    for a in artifacts:
        by_arm.setdefault(str(a["arm"]), []).append(a)
    for arm in by_arm:
        by_arm[arm].sort(key=lambda a: int(a["run"]))

    # Prefer the conventional arm order when both are present.
    arm_order = [a for a in ("arm_graph", "arm_raw") if a in by_arm]
    arm_order += sorted(a for a in by_arm if a not in arm_order)

    counts = {a: len(by_arm[a]) for a in arm_order}
    n_values = set(counts.values())
    balanced = len(n_values) == 1
    if not balanced and not allow_unbalanced:
        raise SystemExit(
            "report: arms have different run counts "
            f"{counts}; refusing a silent lopsided table "
            "(pass --allow-unbalanced to emit anyway, still labelled)"
        )

    # Shared cases_total across runs that reported one (for median X/Y display).
    # Disagreement means the runs were scored against different goldens, so there
    # is no honest denominator -- refuse rather than pick one.
    scored = [
        a for a in artifacts
        if "eval" in a and a["eval"].get("cases_total") not in (None, 0)
    ]
    totals = {int(a["eval"]["cases_total"]) for a in scored}
    if len(totals) > 1:
        listing = ", ".join(
            f"{Path(a['_path']).name}: {a['eval']['cases_total']}" for a in scored
        )
        raise SystemExit(
            f"report: run artifacts disagree on cases_total ({sorted(totals)}); "
            f"they were scored against different goldens -- {listing}"
        )
    cases_total = next(iter(totals)) if totals else None

    arms_out: dict = {}
    rows_md: list[str] = []
    for arm in arm_order:
        runs_a = by_arm[arm]
        scores: list[int] = []
        build_attempts: list = []
        tool_uses: list = []
        wall_s: list = []
        for a in runs_a:
            ev = a.get("eval") or {}
            # Non-building / no score line → 0; real data point, not dropped.
            if "cases_passed" in ev:
                scores.append(int(ev["cases_passed"]))
            elif ev.get("builds") is False or not ev.get("golden_pass", False):
                scores.append(0)
            elif ev.get("golden_pass"):
                # Legacy binary pass with no per-case score: treat as full total if known.
                scores.append(int(cases_total) if cases_total else 1)
            else:
                scores.append(0)
            sr = a.get("self_reported") or {}
            build_attempts.append(sr.get("build_attempts"))
            tool_uses.append(sr.get("tool_uses"))
            wall_s.append(sr.get("wall_s"))

        med = _median([float(s) for s in scores]) if scores else 0.0
        lo = min(scores) if scores else 0
        hi = max(scores) if scores else 0
        scores_str = ", ".join(str(s) for s in scores)
        median_str = f"**{_score_display(med, cases_total)}**"
        minmax_str = f"{lo}–{hi}" if scores else "—"

        # Build attempts: comma list in run order (matches published tables).
        ba_present = [v for v in build_attempts if v is not None]
        ba_str = ",".join(str(int(v)) for v in ba_present) if ba_present else "—"
        tu_str = _range_with_med([float(v) for v in tool_uses if v is not None])
        wall_str = _range_with_med([float(v) for v in wall_s if v is not None])

        arms_out[arm] = {
            "n_runs": len(runs_a),
            "run_indices": [int(a["run"]) for a in runs_a],
            "scores_in_run_order": scores,
            "median": med,
            "median_display": _score_display(med, cases_total),
            "min": lo,
            "max": hi,
            "min_max": minmax_str,
            "cases_total": cases_total,
            "self_reported": {
                "build_attempts": build_attempts,
                "tool_uses": tool_uses,
                "wall_s": wall_s,
                "build_attempts_display": ba_str,
                "tool_uses_display": tu_str,
                "wall_s_display": wall_str,
            },
            "artifacts": [a.get("_path") for a in runs_a],
        }
        rows_md.append(
            f"| **{arm}** | {scores_str} | {median_str} | {minmax_str} "
            f"| {ba_str} | {tu_str} | {wall_str} |"
        )

    # Headers mark self-reported columns — they are agent narrative, not harness.
    header = (
        "| arm | scores | median | min–max "
        "| build attempts (self-reported) | tool-uses (self-reported) | wall (s) (self-reported) |"
    )
    sep = "|---|---|---|---|---|---|---|"
    note_lines = [
        f"Aggregated **{sum(counts.values())}** recorded run(s) "
        f"({', '.join(f'{a}: {counts[a]}' for a in arm_order)}).",
        "Median convention: middle element of the sorted scores for odd *n*; "
        "average of the two middle values for even *n* (`statistics.median`).",
        "Non-building / unscored runs contribute **0** (kept, not dropped).",
        "Columns labelled *(self-reported)* come from the agent narrative "
        "(`eval --self-*`); scores / median / min–max are harness-measured.",
    ]
    if not balanced:
        note_lines.insert(
            1,
            f"**Unbalanced arm counts** {counts} — table emitted with "
            "`--allow-unbalanced`; do not treat as a pre-registered N-per-arm result.",
        )
    markdown = "\n".join([header, sep, *rows_md, "", *note_lines]) + "\n"

    out = {
        "runs_dir": str(runs),
        "n_runs_per_arm": counts,
        "n_runs_total": sum(counts.values()),
        "balanced": balanced,
        "cases_total": cases_total,
        "median_convention": (
            "statistics.median: middle element for odd n; "
            "average of two middle values for even n"
        ),
        "arms": arms_out,
        "markdown": markdown,
    }
    # Markdown first for pasting into PHASE7_*; JSON after for the archive.
    print(markdown)
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    app()
