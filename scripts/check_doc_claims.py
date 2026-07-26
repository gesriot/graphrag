#!/usr/bin/env python
"""Verify load-bearing documented numbers against the artifacts that produce them.

Reads scripts/doc_claims.json. Each claim has:
  - expect: the numbers the docs are allowed to state
  - source: how to derive them (live artifact / frozen snapshot / traced only)
  - docs: paths + substrings that must appear (role current|frozen_snapshot|historical)

Historical claims are *not* checked against current artifacts — only that the
dated record still exists in the document. Frozen-snapshot claims are checked
against a named snapshot id, not against `current`.

Usage:
  uv run python scripts/check_doc_claims.py
  uv run python scripts/check_doc_claims.py --json
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).resolve().parent / "doc_claims.json"


def _count_golden_cases(package: str) -> int:
    tests = ROOT / "examples" / package / "tests"
    if not tests.is_dir():
        raise FileNotFoundError(f"no tests dir for package {package}")
    total = 0
    for p in sorted(tests.rglob("golden_*.json")):
        data = json.loads(p.read_text())
        cases = data.get("cases")
        total += len(cases) if isinstance(cases, list) else 1
    return total


def _graph_counts(graph: str, snapshot: str | None) -> dict[str, int]:
    sys.path.insert(0, str(ROOT / "scripts"))
    import pandas as pd  # type: ignore
    from byog_graph import _resolve_output_base  # type: ignore

    base = ROOT / graph
    if snapshot:
        d = base / "snapshots" / snapshot
        if not d.is_dir():
            raise FileNotFoundError(f"snapshot not found: {d}")
    else:
        d = _resolve_output_base(base)
    ents = pd.read_parquet(d / "entities.parquet")
    rels = pd.read_parquet(d / "relationships.parquet")
    calls = rels[rels["type"].astype(str) == "calls"]
    return {
        "entities": int(len(ents)),
        "relationships": int(len(rels)),
        "calls": int(len(calls)),
    }


def _graph_audit(graph: str) -> dict[str, Any]:
    sys.path.insert(0, str(ROOT / "scripts"))
    from audit_call_edges import build_report  # type: ignore

    report = build_report(ROOT / graph, sample=0)
    return {
        "calls": int(report["total_calls"]),
        "pass_rate": float(report["structural"]["pass_rate"]),
        "anomalies": int(report["structural"]["anomaly_count"]),
        "dangling": int(report["dangling_count"]),
        "semantic_suspicions": int(report.get("semantic_suspicion_count", 0)),
    }


def _pytest_collect(path: str) -> int:
    env = {**dict(**__import__("os").environ), "PYTHONPATH": str(ROOT)}
    res = subprocess.run(
        [sys.executable, "-m", "pytest", path, "--collect-only", "-q"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    out = res.stdout + res.stderr
    m = re.search(r"(\d+) tests? collected", out)
    if not m:
        raise RuntimeError(f"pytest collect failed: {out[-500:]}")
    return int(m.group(1))


def _ablation_adequacy(graph: str, spec: str) -> dict[str, Any]:
    res = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "ablation.py"),
            "adequacy",
            "--graph",
            graph,
            "--spec",
            spec,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise RuntimeError(f"adequacy failed: {res.stderr or res.stdout}")
    data = json.loads(res.stdout)
    return {
        "adequate": bool(data["adequate"]),
        "closure_size": int(data["closure_size"]),
        "must_reach_total": int(data["must_reach_total"]),
        "must_reach_missing": len(data.get("must_reach_missing") or []),
        "must_exclude_leaked": len(data.get("must_exclude_leaked") or []),
    }


def derive(claim: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Return (derived_values or None, status) where status is live|traced|historical."""
    src = claim["source"]
    mode = src.get("mode", "live")
    if claim.get("kind") == "historical" or mode == "traced" and src.get("type") in (
        "none",
        "port_eval",
    ):
        if claim.get("kind") == "historical":
            return None, "historical"
        return None, "traced"

    stype = src["type"]
    if stype == "pytest_collect":
        total = _pytest_collect(src["path"])
        exp = claim["expect"]
        return {
            "collected": total,
            "passed": exp["passed"],
            "xfailed": exp["xfailed"],
            "passed_plus_xfailed": exp["passed"] + exp["xfailed"],
        }, "live"
    if stype == "golden_cases":
        return {"cases": _count_golden_cases(src["package"])}, "live"
    if stype == "graph_counts":
        return _graph_counts(src["graph"], src.get("snapshot")), "live"
    if stype == "graph_audit":
        return _graph_audit(src["graph"]), "live"
    if stype == "ablation_adequacy":
        return _ablation_adequacy(src["graph"], src["spec"]), "live"
    if mode == "traced":
        return None, "traced"
    raise ValueError(f"unknown source type {stype!r} for claim {claim['id']}")


def check_docs(claim: dict[str, Any]) -> list[str]:
    """Return list of doc mismatch messages."""
    errs: list[str] = []
    for doc in claim.get("docs") or []:
        path = ROOT / doc["path"]
        if not path.is_file():
            errs.append(f"{claim['id']}: missing doc {doc['path']}")
            continue
        text = path.read_text()
        needle = doc["must_contain"]
        if needle not in text:
            errs.append(
                f"{claim['id']}: {doc['path']} missing expected text {needle!r} "
                f"(role={doc.get('role', '?')})"
            )
            continue
        near = doc.get("require_near")
        if near:
            # Historical safeguard: the number must sit in the same dated section.
            idx = text.find(needle)
            window = text[max(0, idx - 800) : idx + len(needle) + 200]
            if near not in window and near not in text[: text.find(needle) + 1]:
                # Also accept if the date header appears before the needle in the file
                # within a reasonable distance (same section).
                h = text.rfind(near, 0, idx + 1)
                if h < 0 or idx - h > 2500:
                    errs.append(
                        f"{claim['id']}: {doc['path']} has {needle!r} but not near {near!r}"
                    )
    return errs


def check_expect(claim: dict[str, Any], derived: dict[str, Any] | None) -> list[str]:
    if derived is None:
        return []
    errs: list[str] = []
    exp = claim["expect"]
    src = claim["source"]

    if src["type"] == "pytest_collect":
        # Live invariant: collection size matches the pinned pass+xfail summary.
        want = exp["passed"] + exp["xfailed"]
        got = derived["collected"]
        if got != want:
            errs.append(
                f"{claim['id']}: pytest collected {got} tests in {src['path']!r}, "
                f"but docs pin passed={exp['passed']}+xfailed={exp['xfailed']}={want}"
            )
        return errs

    for key, want in exp.items():
        if key not in derived:
            continue
        got = derived[key]
        if got != want:
            snap = src.get("snapshot")
            where = f"snapshot {snap}" if snap else src.get("graph") or src.get("package") or src["type"]
            errs.append(
                f"{claim['id']}: expect {key}={want!r} but artifact has {got!r} "
                f"(source={src['type']} {where})"
            )
    return errs


def run_all() -> dict[str, Any]:
    manifest = json.loads(MANIFEST.read_text())
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    live = traced = historical = 0

    for claim in manifest["claims"]:
        try:
            derived, status = derive(claim)
        except Exception as e:
            failures.append(f"{claim['id']}: source error: {e}")
            results.append({"id": claim["id"], "status": "error", "error": str(e)})
            continue

        if status == "live":
            live += 1
        elif status == "historical":
            historical += 1
        else:
            traced += 1

        doc_errs = check_docs(claim)
        exp_errs = check_expect(claim, derived)
        ok = not doc_errs and not exp_errs
        if not ok:
            failures.extend(doc_errs)
            failures.extend(exp_errs)
        results.append(
            {
                "id": claim["id"],
                "kind": claim.get("kind"),
                "status": status,
                "ok": ok,
                "derived": derived,
                "expect": claim.get("expect"),
                "errors": doc_errs + exp_errs,
            }
        )

    return {
        "manifest": str(MANIFEST.relative_to(ROOT)),
        "n_claims": len(manifest["claims"]),
        "verified_live": live,
        "traced_only": traced,
        "historical_record": historical,
        "ok": not failures,
        "failures": failures,
        "results": results,
        "left_out": manifest.get("left_out", []),
    }


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="Emit full JSON report")
    args = ap.parse_args()
    report = run_all()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(
            f"doc claims: {report['n_claims']} total — "
            f"live={report['verified_live']} traced={report['traced_only']} "
            f"historical={report['historical_record']}"
        )
        for r in report["results"]:
            flag = "OK " if r["ok"] else "FAIL"
            print(f"  [{flag}] {r['status']:11} {r['id']}")
            for e in r.get("errors") or []:
                print(f"         {e}")
        if report["left_out"]:
            print("left out (by design):")
            for item in report["left_out"]:
                print(f"  - {item['what']}: {item['why']}")
        print("PASS" if report["ok"] else "FAIL")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
