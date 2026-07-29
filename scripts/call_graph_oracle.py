#!/usr/bin/env python
"""Call-graph oracle: compare published ``calls`` edges to traced golden runs.

``audit_call_edges`` checks structural cleanliness (targets resolve, no dangling
edges, sane confidences). It does **not** check that edges are *true*.

This module:

1. Loads ``calls`` edges from a **published** BYOG snapshot (not a fresh extract).
2. Runs the package's golden contract in a **subprocess** under
   ``sys.setprofile``, recording real (caller, callee) pairs inside the package.
3. Reports both directions **separately**:

   * **confirmed** — observed during the contract and present in the graph
   * **missed** — observed, but no graph edge (false negative of the extractor)
   * **unconfirmed** — graph edge never observed (usually contract coverage, not
     a defect — golden suites only exercise a slice of the library)

Neither *missed* nor *unconfirmed* enters an agreement numerator. There is no
pass-rate here that could be mistaken for ``audit_call_edges`` structural
pass_rate.

Usage::

    uv run python scripts/call_graph_oracle.py --package jsonpatch
    uv run python scripts/call_graph_oracle.py --package mini_lang --json
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]

Edge = Tuple[str, str]  # (caller_title, callee_title)


# ---------------------------------------------------------------------------
# Package contracts that can exercise a published graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackageContract:
    """How to locate source, graph, and golden workload for one package."""

    name: str
    package_dir: Path
    graph_dir: Path
    # Identifier understood by the subprocess workload runner.
    workload: str


def known_contracts() -> Dict[str, PackageContract]:
    return {
        "jsonpatch": PackageContract(
            name="jsonpatch",
            package_dir=ROOT / "examples" / "jsonpatch",
            graph_dir=ROOT / "byog_jsonpatch",
            workload="jsonpatch_apply",
        ),
        "mini_lang": PackageContract(
            name="mini_lang",
            package_dir=ROOT / "examples" / "mini_lang",
            graph_dir=ROOT / "byog_mini_lang",
            workload="mini_lang_golden",
        ),
        "humanize": PackageContract(
            name="humanize",
            package_dir=ROOT / "examples" / "humanize",
            graph_dir=ROOT / "byog_humanize",
            workload="humanize_number",
        ),
        "semantic_version": PackageContract(
            name="semantic_version",
            package_dir=ROOT / "examples" / "semantic_version",
            graph_dir=ROOT / "byog_semver",
            workload="semantic_version_golden",
        ),
    }


# ---------------------------------------------------------------------------
# Published graph edges
# ---------------------------------------------------------------------------


def load_published_call_edges(graph_dir: Path) -> Dict[str, Any]:
    """Load ``calls`` edges and entity titles from a published snapshot only."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from byog_graph import ByogGraph  # type: ignore

    graph_dir = Path(graph_dir).resolve()
    if not (graph_dir / "current").is_file() and not (
        graph_dir / "entities.parquet"
    ).is_file():
        raise FileNotFoundError(f"no published BYOG graph at {graph_dir}")
    g = ByogGraph(graph_dir)
    rels = g.rels
    calls = rels[rels["type"].astype(str) == "calls"]
    edges: Set[Edge] = set()
    for _, row in calls.iterrows():
        src = str(row.get("source") or "")
        tgt = str(row.get("target") or "")
        if src and tgt:
            edges.add((src, tgt))
    titles = {str(t) for t in g.ents["title"].astype(str).tolist()}
    snap = None
    current = graph_dir / "current"
    if current.is_file():
        try:
            snap = current.read_text(encoding="utf-8").strip()
        except OSError:
            snap = None
    return {
        "graph_dir": str(graph_dir),
        "snapshot": snap,
        "edges": edges,
        "entity_titles": titles,
        # Relationship *rows* may duplicate a (source, target) pair; oracle
        # scores unique directed edges.
        "n_call_rows": int(len(calls)),
        "n_calls": len(edges),
        "n_entities": len(titles),
    }


# ---------------------------------------------------------------------------
# Subprocess tracer
# ---------------------------------------------------------------------------


def _trace_probe_script() -> str:
    """Self-contained probe: run a named workload under setprofile, emit JSON."""
    return textwrap.dedent(
        r"""
        import json
        import sys
        from pathlib import Path

        package_dir = Path(sys.argv[1]).resolve()
        workload = sys.argv[2]
        entity_titles = set(json.loads(sys.argv[3]))

        # Map source files under the package to the module prefix used in titles
        # (e.g. examples/jsonpatch/jsonpatch.py -> "jsonpatch",
        #  examples/mini_lang/eval.py -> "eval").
        file_to_mod: dict[str, str] = {}
        # Prefix groups from entity titles: "jsonpatch", "eval", "errors", …
        prefixes = {t.split(":", 1)[0] for t in entity_titles if ":" in t}

        for path in package_dir.rglob("*.py"):
            if any(p in {"__pycache__", "tests", ".venv", "venv"} for p in path.parts):
                continue
            try:
                rel = path.relative_to(package_dir)
            except ValueError:
                continue
            parts = list(rel.with_suffix("").parts)
            if rel.name == "__init__.py":
                # package root or subpackage __init__
                if len(parts) <= 1:
                    mod_guess = package_dir.name
                    parts = [package_dir.name]
                else:
                    # subpkg/__init__.py -> package.subpkg
                    parts = [package_dir.name] + parts[:-1]
                    mod_guess = ".".join(parts)
            else:
                if (package_dir / "__init__.py").is_file():
                    mod_guess = ".".join([package_dir.name] + parts)
                else:
                    mod_guess = ".".join(parts)
            # Prefer a prefix that actually appears on graph entities.
            stem = parts[-1] if parts else package_dir.name
            candidates = [mod_guess, stem, package_dir.name]
            chosen = None
            for c in candidates:
                if c in prefixes:
                    chosen = c
                    break
            if chosen is None:
                chosen = stem
            file_to_mod[str(path.resolve())] = chosen
            # Also key by the path Python may report (symlinks, etc.)
            file_to_mod[str(path)] = chosen

        def frame_raw_title(frame) -> str | None:
            if frame is None:
                return None
            code = frame.f_code
            filename = code.co_filename
            if not filename or filename.startswith("<"):
                return None
            try:
                resolved = str(Path(filename).resolve())
            except OSError:
                resolved = filename
            mod = file_to_mod.get(resolved) or file_to_mod.get(filename)
            if mod is None:
                # Allow path containment match for .pyc / zip edge cases.
                for fpath, m in file_to_mod.items():
                    if resolved.endswith(fpath) or fpath.endswith(resolved):
                        mod = m
                        break
            if mod is None:
                return None
            qual = getattr(code, "co_qualname", None) or code.co_name
            if not qual or qual in {"<module>", "<lambda>", "<listcomp>", "<genexpr>",
                                     "<setcomp>", "<dictcomp>", "<genexpr>"}:
                return None
            return f"{mod}:{qual}"

        def resolve_title(raw: str | None) -> str | None:
            if not raw:
                return None
            # Graph edges target the *class* for constructors
            # (``eval:run -> eval:Interpreter``), while the profiler sees
            # ``Interpreter.__init__``. Prefer the class entity when both exist.
            for suffix in (".__init__", ".__new__", ".__call__"):
                if raw.endswith(suffix):
                    base = raw[: -len(suffix)]
                    if base in entity_titles:
                        return base
            if raw in entity_titles:
                return raw
            return raw

        observed_raw: set[tuple[str, str]] = set()
        observed_mapped: set[tuple[str, str]] = set()
        unmapped: list[dict] = []

        def profile(frame, event, arg):
            if event != "call":
                return
            caller = frame.f_back
            c_raw = frame_raw_title(caller)
            e_raw = frame_raw_title(frame)
            if not c_raw or not e_raw:
                return
            if c_raw == e_raw:
                return
            observed_raw.add((c_raw, e_raw))
            c_m = resolve_title(c_raw)
            e_m = resolve_title(e_raw)
            if c_m is None or e_m is None:
                return
            # Both ends must land on published entities to score as graph edges.
            if c_m in entity_titles and e_m in entity_titles:
                observed_mapped.add((c_m, e_m))
            else:
                if len(unmapped) < 80:
                    unmapped.append({
                        "caller": c_raw,
                        "callee": e_raw,
                        "caller_mapped": c_m if c_m in entity_titles else None,
                        "callee_mapped": e_m if e_m in entity_titles else None,
                    })

        # (golden file, executed case count) per workload, so a workload that
        # silently ran nothing — or ran a substitute corpus — is visible in the
        # report instead of reading as a clean measurement.
        _workload_cases = []

        def run_jsonpatch_apply():
            sys.path.insert(0, str(package_dir))
            import jsonpatch  # noqa: WPS433
            golden = package_dir / "tests" / "apply" / "golden_apply.json"
            cases = json.loads(golden.read_text())["cases"]
            for c in cases:
                doc = json.loads(c["doc"])
                patch = json.loads(c["patch"])
                try:
                    jsonpatch.apply_patch(doc, patch)
                except Exception:
                    pass
            _workload_cases.append((golden.name, len(cases)))

        def run_mini_lang_golden():
            sys.path.insert(0, str(package_dir))
            from main import run_source  # noqa: WPS433
            for gf in sorted((package_dir / "tests").glob("golden_*.json")):
                data = json.loads(gf.read_text())
                for c in data["cases"]:
                    try:
                        run_source(c["source"])
                    except Exception:
                        pass
                _workload_cases.append((gf.name, len(data["cases"])))

        def run_humanize_number():
            sys.path.insert(0, str(package_dir.parent))  # import humanize package
            # Prefer package dir on path so vendored humanize wins.
            sys.path.insert(0, str(package_dir))
            import humanize.number as number  # noqa: WPS433
            # Locate the golden by search, not by a guessed path: it lives at
            # tests/number/golden_number.json, and a hard-coded tests/ path
            # silently selected a hand-written smoke set instead.
            found = sorted((package_dir / "tests").rglob("golden_number.json"))
            if not found:
                raise RuntimeError(
                    "humanize golden_number.json not found under tests/; refusing "
                    "to substitute a smoke set, which would measure something else"
                )
            cases = json.loads(found[0].read_text()).get("cases") or []
            ran = 0
            for c in cases:
                # The golden uses "func"; earlier revisions used fn/function/name.
                name = c.get("func") or c.get("fn") or c.get("function") or c.get("name")
                if not name or not hasattr(number, name):
                    continue
                try:
                    getattr(number, name)(*(c.get("args") or []), **(c.get("kwargs") or {}))
                except Exception:
                    pass
                ran += 1
            if not ran:
                raise RuntimeError(
                    f"humanize golden at {found[0]} produced 0 executable cases "
                    f"({len(cases)} present); the case shape changed"
                )
            _workload_cases.append((str(found[0].name), ran))

        def run_semantic_version_golden():
            sys.path.insert(0, str(package_dir.parent))
            sys.path.insert(0, str(package_dir))
            import semantic_version as sv  # noqa: WPS433
            for gf in sorted((package_dir / "tests").glob("golden_*.json")):
                data = json.loads(gf.read_text())
                for c in data.get("cases") or []:
                    try:
                        # common shapes: compare / coerce / match
                        if "version" in c and "op" not in c:
                            sv.Version.coerce(c["version"]) if hasattr(sv.Version, "coerce") else sv.Version(c["version"])
                        if "a" in c and "b" in c:
                            va, vb = sv.Version(c["a"]), sv.Version(c["b"])
                            _ = (va == vb, va < vb, va > vb)
                        if "spec" in c:
                            spec = sv.SimpleSpec(c["spec"]) if hasattr(sv, "SimpleSpec") else sv.Spec(c["spec"])
                            if "version" in c:
                                spec.match(sv.Version(c["version"]))
                    except Exception:
                        pass

        runners = {
            "jsonpatch_apply": run_jsonpatch_apply,
            "mini_lang_golden": run_mini_lang_golden,
            "humanize_number": run_humanize_number,
            "semantic_version_golden": run_semantic_version_golden,
        }
        if workload not in runners:
            print(json.dumps({"ok": False, "error": f"unknown workload {workload!r}"}))
            raise SystemExit(0)

        sys.setprofile(profile)
        try:
            runners[workload]()
        finally:
            sys.setprofile(None)

        print(json.dumps({
            "ok": True,
            "workload": workload,
            "observed_raw": sorted([list(p) for p in observed_raw]),
            "observed_mapped": sorted([list(p) for p in observed_mapped]),
            "n_observed_raw": len(observed_raw),
            "n_observed_mapped": len(observed_mapped),
            "workload_cases": [list(x) for x in _workload_cases],
            "n_workload_cases": sum(n for _f, n in _workload_cases),
            "unmapped_samples": unmapped[:40],
        }))
        """
    ).strip()


def trace_contract_calls(
    package_dir: Path,
    workload: str,
    entity_titles: Iterable[str],
    *,
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """Run *workload* under setprofile in a subprocess; return observed pairs."""
    package_dir = Path(package_dir).resolve()
    titles_json = json.dumps(sorted(set(entity_titles)))
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                _trace_probe_script(),
                str(package_dir),
                workload,
                titles_json,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(package_dir),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"trace timeout after {timeout}s"}
    except OSError as e:
        return {"ok": False, "error": f"OSError: {e}"}

    stdout = (proc.stdout or "").strip()
    if not stdout:
        err = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return {"ok": False, "error": f"empty trace output: {err}"}
    line = stdout.splitlines()[-1]
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "error": (
                f"trace returned non-JSON: {line[:240]!r}; "
                f"stderr={(proc.stderr or '')[:240]!r}"
            ),
        }
    if not isinstance(data, dict):
        return {"ok": False, "error": "trace returned non-object JSON"}
    if not data.get("ok"):
        return data
    # Normalize lists to tuples.
    data["observed_mapped"] = {
        (a, b) for a, b in (data.get("observed_mapped") or [])
    }
    data["observed_raw"] = {
        (a, b) for a, b in (data.get("observed_raw") or [])
    }
    return data


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def score_call_edges(
    graph_edges: Set[Edge],
    observed: Set[Edge],
    *,
    n_call_rows: Optional[int] = None,
    n_observed_raw: Optional[int] = None,
    unmapped_samples: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Score unique directed edges: confirmed / missed / unconfirmed.

    Pure function — no I/O. Used by the oracle and by plant tests that mutate
    the edge set without re-tracing.
    """
    confirmed = sorted(observed & graph_edges)
    missed = sorted(observed - graph_edges)
    unconfirmed = sorted(graph_edges - observed)
    return {
        "n_graph_call_rows": int(n_call_rows if n_call_rows is not None else len(graph_edges)),
        "n_graph_calls": len(graph_edges),
        "n_observed_mapped": len(observed),
        "n_observed_raw": int(
            n_observed_raw if n_observed_raw is not None else len(observed)
        ),
        "confirmed": len(confirmed),
        "missed": len(missed),
        "unconfirmed": len(unconfirmed),
        "confirmed_edges": [{"caller": a, "callee": b} for a, b in confirmed[:80]],
        "missed_edges": [{"caller": a, "callee": b} for a, b in missed[:80]],
        "unconfirmed_edges": [
            {"caller": a, "callee": b} for a, b in unconfirmed[:80]
        ],
        "unmapped_samples": list(unmapped_samples or [])[:40],
        # Informational only — unconfirmed is coverage, not a defect; missed is
        # an extractor gap. Neither is folded into a single agreement score.
        "coverage_of_graph": (
            (len(confirmed) / len(graph_edges)) if graph_edges else None
        ),
        "recall_of_observed": (
            (len(confirmed) / len(observed)) if observed else None
        ),
    }


def compare_call_edges_to_trace(
    graph_dir: Path,
    package_dir: Path,
    *,
    workload: str,
    edges: Optional[Set[Edge]] = None,
    entity_titles: Optional[Set[str]] = None,
    timeout: float = 120.0,
) -> Dict[str, Any]:
    """Compare published (or injected) call edges to a traced golden run.

    Parameters
    ----------
    edges, entity_titles:
        Optional overrides for tests (plant a deleted / fabricated edge without
        rewriting the snapshot on disk).
    """
    published = load_published_call_edges(graph_dir)
    graph_edges: Set[Edge] = set(edges) if edges is not None else set(published["edges"])
    titles: Set[str] = (
        set(entity_titles) if entity_titles is not None else set(published["entity_titles"])
    )

    report: Dict[str, Any] = {
        "package": str(Path(package_dir).resolve()),
        "graph_dir": str(Path(graph_dir).resolve()),
        "snapshot": published.get("snapshot"),
        "workload": workload,
        "status": "ok",
        "skip_reason": None,
        "ok": True,
    }

    trace = trace_contract_calls(
        package_dir, workload, titles, timeout=timeout
    )
    if not trace.get("ok"):
        report["status"] = "skipped"
        report["skip_reason"] = str(trace.get("error") or "trace failed")
        report.update(
            score_call_edges(
                graph_edges,
                set(),
                n_call_rows=int(published.get("n_call_rows") or len(graph_edges)),
            )
        )
        return report

    observed: Set[Edge] = set(trace.get("observed_mapped") or set())
    scored = score_call_edges(
        graph_edges,
        observed,
        n_call_rows=int(published.get("n_call_rows") or len(graph_edges)),
        n_observed_raw=int(
            trace.get("n_observed_raw") or len(trace.get("observed_raw") or [])
        ),
        unmapped_samples=list(trace.get("unmapped_samples") or []),
    )
    report.update(scored)
    # Carry through what the workload actually executed, so a run that drove
    # zero golden cases cannot read as a clean measurement.
    report["workload_cases"] = list(trace.get("workload_cases") or [])
    report["n_workload_cases"] = int(trace.get("n_workload_cases") or 0)
    # ok means the measurement ran; missed edges are reported, not failures.
    report["ok"] = True
    return report


def format_call_oracle_report(report: Dict[str, Any]) -> str:
    def pct(x: Optional[float]) -> str:
        if x is None:
            return "n/a"
        return f"{100.0 * x:.1f}%"

    lines = [
        f"Call-graph oracle: {report.get('package')}",
        f"  graph                   : {report.get('graph_dir')} "
        f"(snapshot={report.get('snapshot')})",
        f"  workload                : {report.get('workload')}",
        f"  status                  : {report.get('status')}"
        + (f" ({report.get('skip_reason')})" if report.get("skip_reason") else ""),
        f"  graph calls (published) : {report.get('n_graph_calls')} unique "
        f"({report.get('n_graph_call_rows')} relationship rows)",
        f"  workload cases executed : {report.get('n_workload_cases')} "
        f"{[f'{f}:{n}' for f, n in (report.get('workload_cases') or [])]}",
        f"  observed (mapped)       : {report.get('n_observed_mapped')} "
        f"(raw pairs={report.get('n_observed_raw')})",
        f"  confirmed (both)        : {report.get('confirmed')}",
        f"  missed (observed only)  : {report.get('missed')}",
        f"  unconfirmed (graph only): {report.get('unconfirmed')}",
        f"  coverage of graph       : {pct(report.get('coverage_of_graph'))} "
        f"(confirmed/graph; unconfirmed ≠ wrong)",
        f"  recall of observed      : {pct(report.get('recall_of_observed'))} "
        f"(confirmed/observed; missed = extractor gap)",
    ]
    if report.get("missed_edges"):
        lines.append("  missed edges (sample):")
        for e in (report["missed_edges"] or [])[:12]:
            lines.append(f"    {e['caller']} -> {e['callee']}")
    if report.get("unconfirmed_edges"):
        lines.append("  unconfirmed edges (sample):")
        for e in (report["unconfirmed_edges"] or [])[:12]:
            lines.append(f"    {e['caller']} -> {e['callee']}")
    if report.get("unmapped_samples"):
        lines.append("  unmapped observed (sample; not scored as missed):")
        for u in (report["unmapped_samples"] or [])[:8]:
            lines.append(f"    {u.get('caller')} -> {u.get('callee')}")
    lines.append(
        "  note: measurement only — does not modify graphs, is_deterministic, "
        "or audit_call_edges pass rates. unconfirmed is coverage, not a defect."
    )
    return "\n".join(lines)


def compare_named_package(name: str, **kwargs: Any) -> Dict[str, Any]:
    contracts = known_contracts()
    if name not in contracts:
        raise KeyError(
            f"unknown package {name!r}; known: {sorted(contracts)}"
        )
    c = contracts[name]
    return compare_call_edges_to_trace(
        c.graph_dir,
        c.package_dir,
        workload=c.workload,
        **kwargs,
    )


def main(argv: Optional[Sequence[str]] = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--package",
        "-p",
        required=True,
        help=f"package name or path; known: {', '.join(sorted(known_contracts()))}",
    )
    ap.add_argument(
        "--graph",
        type=Path,
        default=None,
        help="override published BYOG graph dir",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="subprocess trace timeout seconds",
    )
    args = ap.parse_args(argv)

    contracts = known_contracts()
    key = args.package
    if key in contracts:
        c = contracts[key]
        graph = args.graph or c.graph_dir
        report = compare_call_edges_to_trace(
            graph, c.package_dir, workload=c.workload, timeout=args.timeout
        )
    else:
        # Treat as a package path; require --graph and infer workload if known.
        pkg = Path(key).resolve()
        if args.graph is None:
            ap.error("when --package is a path, --graph is required")
        # Guess workload by name.
        workload = "jsonpatch_apply"
        if pkg.name == "mini_lang":
            workload = "mini_lang_golden"
        elif pkg.name == "humanize":
            workload = "humanize_number"
        elif pkg.name == "semantic_version":
            workload = "semantic_version_golden"
        report = compare_call_edges_to_trace(
            args.graph, pkg, workload=workload, timeout=args.timeout
        )

    if args.json:
        # Sets are not JSON-serializable; report already uses lists/counts.
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_call_oracle_report(report))
    raise SystemExit(0 if report.get("status") != "error" else 1)


if __name__ == "__main__":
    main()
