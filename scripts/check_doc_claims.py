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
import importlib.util
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from evidence_durability import claim_durability

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


def _has_complete_graph(root: Path) -> bool:
    """Whether a graph root contains the three BYOG tables an audit needs."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from byog_graph import _resolve_output_base  # type: ignore

    for candidate in (root, root / "output"):
        base = _resolve_output_base(candidate)
        if all((base / name).is_file() for name in (
            "entities.parquet", "relationships.parquet", "text_units.parquet"
        )):
            return True
    return False


def _graph_audit(graph: str, fallback_graph: str | None = None) -> dict[str, Any]:
    """Audit the published graph, or the fresh gate graph when none is present."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from audit_call_edges import build_report  # type: ignore

    candidates = [ROOT / graph]
    if fallback_graph is not None:
        candidates.append(ROOT / fallback_graph)
    graph_root = next((path for path in candidates if _has_complete_graph(path)), None)
    if graph_root is None:
        rendered = ", ".join(str(path) for path in candidates)
        raise FileNotFoundError(f"no complete graph artifact found: {rendered}")
    report = build_report(graph_root, sample=0)
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


def _cjson_api_surface() -> dict[str, int]:
    """Derive the cJSON audit counts from its header-backed audit program."""
    audit_path = ROOT / "examples" / "cjson" / "tools" / "api_surface_audit.py"
    spec = importlib.util.spec_from_file_location("cjson_api_surface_audit", audit_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load cJSON API audit from {audit_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    functions, data = module.parse_header(module.DEFAULT_HEADER)
    module.validate_manifest(functions, data)
    return {
        "functions": len(functions),
        "public_data": len(data),
        "covered_functions": len(module.COVERED_FUNCTIONS),
        "ownership_blocked": len(module.OWNERSHIP_BLOCKED),
        "global_state_excluded": len(module.GLOBAL_STATE_EXCLUDED),
    }


def _port_gate_manifest() -> dict[str, int]:
    """Count the declared port profiles and gaps, validating the manifest."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from port_eval import load_gate_manifest  # type: ignore

    gates = load_gate_manifest(ROOT / "scripts" / "port_gates.json")
    return {
        "profiles": sum(
            1 for e in gates.values() if e.get("kind", "port") == "port"
        ),
        "declared_gaps": sum(1 for e in gates.values() if e.get("kind") == "gap"),
    }


def _oracle_residuals() -> dict[str, int]:
    """Derive the four-oracle residual table through its public summary adapter."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from oracle_summary import build_report  # type: ignore

    report = build_report()
    if not report.get("ok"):
        raise RuntimeError("combined oracle summary did not report a successful measurement")
    residuals = report["residuals"]
    return {
        "c_preprocessor_unknown": int(residuals["c_preprocessor_unknown"]),
        "c_preprocessor_vacuous": int(residuals["c_preprocessor_vacuous"]),
        "python_registry_missed": int(residuals["python_registry_missed"]),
        "call_graph_missed": int(residuals["call_graph_missed"]),
        "call_graph_unconfirmed": int(residuals["call_graph_unconfirmed"]),
        "cjson_ownership_blocked": int(residuals["cjson_ownership_blocked"]),
        "cjson_global_state_excluded": int(residuals["cjson_global_state_excluded"]),
    }


def _inherited_member_runtime_audit() -> dict[str, int]:
    """Derive the cross-package inherited-member result from imported runtime."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from inherited_member_runtime_audit import build_report  # type: ignore

    report = build_report()
    if not report.get("ok"):
        raise RuntimeError("inherited-member runtime audit reported a mismatch or no candidates")
    packages = {str(row["package"]): row for row in report["packages"]}
    try:
        sqlparse = packages["sqlparse"]
        semantic_version = packages["semantic_version"]
    except KeyError as exc:
        raise RuntimeError(f"runtime audit omitted required package: {exc}") from exc
    totals = report["totals"]
    return {
        "sqlparse_candidates": int(sqlparse["candidates"]),
        "sqlparse_confirmed": int(sqlparse["confirmed"]),
        "semantic_version_candidates": int(semantic_version["candidates"]),
        "semantic_version_confirmed": int(semantic_version["confirmed"]),
        "total_candidates": int(totals["candidates"]),
        "total_confirmed": int(totals["confirmed"]),
        "mismatches": int(totals["mismatches"]),
        "errors": int(totals["errors"]),
        "sqlparse_multiple_inheritance": int(
            sqlparse["runtime_shapes"]["multiple_inheritance_candidates"]
        ),
        "sqlparse_slotted_children": int(
            sqlparse["runtime_shapes"]["slotted_child_candidates"]
        ),
        "sqlparse_properties": int(sqlparse["runtime_shapes"]["property_candidates"]),
        "semantic_version_slotted_children": int(
            semantic_version["runtime_shapes"]["slotted_child_candidates"]
        ),
    }


def _initializer_api_runtime_audit() -> dict[str, int]:
    """Derive initializer API coverage from runtime names and fresh graph titles."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from init_api_runtime_audit import build_report  # type: ignore

    report = build_report()
    if not report.get("ok"):
        raise RuntimeError("initializer API runtime audit reported a missing direct definition")
    totals = report["totals"]
    return {
        "python_targets": int(totals["python_targets"]),
        "targets_with_initializer": int(totals["targets_with_initializer"]),
        "initializer_modules": int(totals["initializer_modules"]),
        "public_names": int(totals["public_names"]),
        "direct_definitions": int(totals["direct_definitions"]),
        "direct_present": int(totals["direct_present"]),
        "direct_missing": int(totals["direct_missing"]),
        "reexports": int(totals["reexports"]),
        "reexport_present": int(totals["reexport_present"]),
        "reexport_missing": int(totals["reexport_missing"]),
        "runtime_errors": int(totals["runtime_errors"]),
    }


def _reexport_reachability_audit() -> dict[str, int]:
    """Derive the static-binding and traced-target residual populations."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from reexport_reachability_audit import build_report  # type: ignore

    report = build_report()
    if not report.get("ok"):
        raise RuntimeError("re-export reachability audit reported a runtime or trace failure")
    totals = report["totals"]
    return {
        "reexports": int(totals["reexports"]),
        "target_resolved": int(totals["target_resolved"]),
        "target_ambiguous": int(totals["target_ambiguous"]),
        "target_initializer_not_indexed": int(totals["target_initializer_not_indexed"]),
        "target_no_source_identity": int(totals["target_no_source_identity"]),
        "initializer_modules": int(totals["initializer_modules"]),
        "exporting_initializer_module_entities": int(
            totals["exporting_initializer_module_entities"]
        ),
        "exporting_initializer_module_nodes_needed": int(
            totals["exporting_initializer_module_nodes_needed"]
        ),
        "identity_export_edges_if_modules_added": int(
            totals["identity_export_edges_if_modules_added"]
        ),
        "traced_reexports": int(totals["traced_reexports"]),
        "untraced_reexports": int(totals["untraced_reexports"]),
        "resolved_targets_observed": int(totals["resolved_targets_observed"]),
        "traced_workloads": int(totals["traced_workloads"]),
        "traced_cases": int(totals["traced_cases"]),
        "humanize_observed_targets": int(totals["humanize_observed_targets"]),
        "semantic_version_observed_targets": int(
            totals["semantic_version_observed_targets"]
        ),
        "sqlparse_observed_targets": int(totals["sqlparse_observed_targets"]),
        "runtime_errors": int(totals["runtime_errors"]),
        "trace_errors": int(totals["trace_errors"]),
    }


def _call_graph_oracle(package: str) -> dict[str, int]:
    """Derive a named local-graph call-oracle measurement without a fallback."""
    sys.path.insert(0, str(ROOT / "scripts"))
    from call_graph_oracle import compare_named_package  # type: ignore

    report = compare_named_package(package)
    if report.get("status") != "ok":
        raise RuntimeError(
            f"call oracle for {package} did not run: {report.get('skip_reason') or report}"
        )
    workloads = dict(report.get("workload_cases") or [])
    return {
        "graph_call_rows": int(report["n_graph_call_rows"]),
        "observed_mapped": int(report["n_observed_mapped"]),
        "observed_raw": int(report["n_observed_raw"]),
        "confirmed": int(report["confirmed"]),
        "missed": int(report["missed"]),
        "unconfirmed": int(report["unconfirmed"]),
        "lex_cases": int(workloads.get("tests/lex/golden_lex.json", 0)),
        "split_cases": int(workloads.get("tests/split/golden_split.json", 0)),
    }


def _frozen_source_missing(claim: dict[str, Any]) -> bool:
    """Recognize an absent protected snapshot without masking a damaged one.

    Clean clones deliberately do not include the large SQLParse snapshots or the
    frozen isodate ablation graph.  A missing root/snapshot is therefore an
    explicit skip.  Once a root or requested snapshot exists, normal derivation
    is still required and any missing parquet or malformed data fails.
    """
    if claim.get("kind") != "frozen_snapshot":
        return False
    source = claim["source"]
    stype = source["type"]
    if stype == "graph_counts":
        snapshot = source.get("snapshot")
        if not isinstance(snapshot, str):
            return False
        return not (ROOT / source["graph"] / "snapshots" / snapshot).is_dir()
    if stype == "ablation_adequacy":
        return not (ROOT / source["graph"]).exists()
    return False


def _optional_source_missing(claim: dict[str, Any]) -> bool:
    """Whether a manifest-declared regenerable source is unavailable locally."""
    source = claim["source"]
    if source.get("allow_missing") is not True:
        return False
    if source["type"] != "graph_audit":
        return False
    candidates = [ROOT / source["graph"]]
    fallback = source.get("fallback_graph")
    if isinstance(fallback, str):
        candidates.append(ROOT / fallback)
    return not any(_has_complete_graph(path) for path in candidates)


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
        return _graph_audit(src["graph"], src.get("fallback_graph")), "live"
    if stype == "ablation_adequacy":
        return _ablation_adequacy(src["graph"], src["spec"]), "live"
    if stype == "cjson_api_surface":
        return _cjson_api_surface(), "live"
    if stype == "port_gate_manifest":
        return _port_gate_manifest(), "live"
    if stype == "oracle_residuals":
        return _oracle_residuals(), "live"
    if stype == "inherited_member_runtime_audit":
        return _inherited_member_runtime_audit(), "live"
    if stype == "initializer_api_runtime_audit":
        return _initializer_api_runtime_audit(), "live"
    if stype == "reexport_reachability_audit":
        return _reexport_reachability_audit(), "live"
    if stype == "call_graph_oracle":
        return _call_graph_oracle(src["package"]), "live"
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
    live = traced = historical = frozen_unavailable = source_unavailable = 0
    durability_tiers: Counter[str] = Counter()

    for claim in manifest["claims"]:
        try:
            durability = claim_durability(claim)
        except (TypeError, ValueError) as error:
            failures.append(f"{claim.get('id', '<unknown>')}: durability metadata error: {error}")
            results.append(
                {
                    "id": claim.get("id", "<unknown>"),
                    "kind": claim.get("kind"),
                    "status": "error",
                    "ok": False,
                    "error": str(error),
                    "errors": [str(error)],
                }
            )
            continue
        durability_tiers[str(durability["tier"])] += 1
        skip_reason: str | None = None
        if _frozen_source_missing(claim):
            skip_reason = "protected frozen source is absent from this checkout"
        elif _optional_source_missing(claim):
            skip_reason = "regenerable source is absent; run its port gate to produce fresh evidence"
        if skip_reason is not None:
            doc_errs = check_docs(claim)
            ok = not doc_errs
            if not ok:
                failures.extend(doc_errs)
            if claim.get("kind") == "frozen_snapshot":
                frozen_unavailable += 1
            source_unavailable += 1
            results.append(
                {
                    "id": claim["id"],
                    "kind": claim.get("kind"),
                    "status": "skipped",
                    "ok": ok,
                    "reason": skip_reason,
                    "durability": durability,
                    "derived": None,
                    "expect": claim.get("expect"),
                    "errors": doc_errs,
                }
            )
            continue
        try:
            derived, status = derive(claim)
        except Exception as e:
            failures.append(f"{claim['id']}: source error: {e}")
            results.append(
                {
                    "id": claim["id"],
                    "kind": claim.get("kind"),
                    "status": "error",
                    "ok": False,
                    "error": str(e),
                    "durability": durability,
                    "errors": [f"{claim['id']}: source error: {e}"],
                }
            )
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
                "durability": durability,
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
        "frozen_source_skips": frozen_unavailable,
        "source_skips": source_unavailable,
        "durability_tiers": dict(sorted(durability_tiers.items())),
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
            f"historical={report['historical_record']} "
            f"source-skips={report['source_skips']} "
            f"frozen-source-skips={report['frozen_source_skips']}"
        )
        print(
            "evidence durability: "
            + ", ".join(
                f"{tier}={count}" for tier, count in report["durability_tiers"].items()
            )
        )
        for r in report["results"]:
            flag = "SKIP" if r["status"] == "skipped" else ("OK " if r["ok"] else "FAIL")
            print(f"  [{flag}] {r['status']:11} {r['id']}")
            durability = r.get("durability")
            if isinstance(durability, dict):
                print(
                    f"         evidence={durability['tier']}; "
                    f"replay={durability['replay']}"
                )
            if r.get("reason"):
                print(f"         {r['reason']}")
            for e in r.get("errors") or []:
                print(f"         {e}")
        if report["left_out"]:
            print("left out (by design):")
            for item in report["left_out"]:
                print(f"  - {item['what']}: {item['why']}")
        if report["ok"] and report["source_skips"]:
            print("PASS WITH SOURCE SKIPS")
        else:
            print("PASS" if report["ok"] else "FAIL")
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
