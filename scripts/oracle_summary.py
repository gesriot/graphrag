#!/usr/bin/env python
"""Run the repository's independent evidence oracles and show their residuals.

This is deliberately an adapter, not an oracle framework.  Each child retains
its own population, comparator, and failure policy; this script invokes the
three JSON CLIs, runs cJSON's fail-closed header check, and only collates their
already-derived reports.

Run from the repository root:

    uv run python scripts/oracle_summary.py
    uv run python scripts/oracle_summary.py --json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
# The summary owns this selection; durability reporting imports it instead of
# duplicating a package name while classifying the aggregate doc claim.
CALL_GRAPH_ORACLE_PACKAGE = "jsonpatch"


class OracleRunError(RuntimeError):
    """An oracle could not produce its own report; no replacement is allowed."""


def _run_json(name: str, argv: Sequence[str]) -> dict[str, Any]:
    """Execute one oracle's public JSON interface, failing with its real output."""
    proc = subprocess.run(
        list(argv),
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise OracleRunError(
            f"{name} exited {proc.returncode}: {' '.join(argv)}"
            + (f"\n{detail}" if detail else "")
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        raise OracleRunError(
            f"{name} did not emit one JSON report: {error}\n{proc.stdout[-800:]}"
        ) from error
    if not isinstance(payload, dict):
        raise OracleRunError(f"{name} JSON report is not an object")
    return payload


def _cjson_surface_report() -> dict[str, int]:
    """Run the public audit check, then read its header-derived classifications."""
    audit = ROOT / "examples" / "cjson" / "tools" / "api_surface_audit.py"
    proc = subprocess.run(
        [sys.executable, str(audit), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise OracleRunError(
            f"cJSON API surface exited {proc.returncode}: {sys.executable} {audit} --check"
            + (f"\n{detail}" if detail else "")
        )

    spec = importlib.util.spec_from_file_location("cjson_api_surface_audit", audit)
    if spec is None or spec.loader is None:
        raise OracleRunError(f"could not load cJSON API audit at {audit}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    functions, public_data = module.parse_header(module.DEFAULT_HEADER)
    module.validate_manifest(functions, public_data)
    covered = len(module.COVERED_FUNCTIONS)
    ownership_blocked = len(module.OWNERSHIP_BLOCKED)
    global_state_excluded = len(module.GLOBAL_STATE_EXCLUDED)
    if covered + ownership_blocked + global_state_excluded != len(functions):
        raise OracleRunError(
            "cJSON API classifications do not partition the parsed header functions"
        )
    return {
        "functions": len(functions),
        "covered": covered,
        "ownership_blocked": ownership_blocked,
        "global_state_excluded": global_state_excluded,
    }


def build_report() -> dict[str, Any]:
    """Run all four independent oracles and return their native residual counts."""
    preprocessor = _run_json(
        "C preprocessor liveness",
        [
            sys.executable,
            str(ROOT / "scripts" / "c_preprocessor.py"),
            "--package",
            "examples/cjson",
            "--vs-compiler",
            "--json",
        ],
    )
    registry = _run_json(
        "Python registry",
        [
            sys.executable,
            str(ROOT / "scripts" / "python_dynamic.py"),
            "--package",
            "examples/isodate",
            "--vs-runtime",
            "--json",
        ],
    )
    call_graph = _run_json(
        "call-graph observation",
        [
            sys.executable,
            str(ROOT / "scripts" / "call_graph_oracle.py"),
            "--package",
            CALL_GRAPH_ORACLE_PACKAGE,
            "--json",
        ],
    )
    cjson_surface = _cjson_surface_report()

    return {
        "ok": bool(preprocessor.get("ok"))
        and registry.get("status") == "ok"
        and bool(registry.get("ok"))
        and call_graph.get("status") == "ok"
        and bool(call_graph.get("ok")),
        "oracles": {
            "c_preprocessor_liveness": preprocessor,
            "python_registry": registry,
            "call_graph_observation": call_graph,
            "cjson_api_surface": cjson_surface,
        },
        "residuals": {
            "c_preprocessor_unknown": int(preprocessor["regions_unknown"]),
            "c_preprocessor_vacuous": int(preprocessor["regions_vacuous"]),
            "python_registry_missed": int(registry["missed"]),
            "call_graph_missed": int(call_graph["missed"]),
            "call_graph_unconfirmed": int(call_graph["unconfirmed"]),
            "cjson_ownership_blocked": cjson_surface["ownership_blocked"],
            "cjson_global_state_excluded": cjson_surface["global_state_excluded"],
        },
    }


def format_report(report: dict[str, Any]) -> str:
    """Render a compact comparison without collapsing the native categories."""
    sources = report["oracles"]
    preprocessor = sources["c_preprocessor_liveness"]
    registry = sources["python_registry"]
    call_graph = sources["call_graph_observation"]
    surface = sources["cjson_api_surface"]
    residuals = report["residuals"]
    return "\n".join(
        [
            "Independent-oracle summary (native categories are intentionally separate)",
            "  C preprocessor liveness",
            "    judged={scored} agreement={agreements} disagreement={disagreements}; "
            "unknown={unknown} vacuous={vacuous}".format(
                scored=preprocessor["regions_scored"],
                agreements=preprocessor["agreements"],
                disagreements=preprocessor["disagreements"],
                unknown=residuals["c_preprocessor_unknown"],
                vacuous=residuals["c_preprocessor_vacuous"],
            ),
            "  Python registry runtime oracle",
            "    runtime={runtime} scored={scored} agree={agreements} "
            "disagree={disagreements}; missed={missed}".format(
                runtime=registry["entries_runtime"],
                scored=registry["entries_scored"],
                agreements=registry["agreements"],
                disagreements=registry["disagreements"],
                missed=residuals["python_registry_missed"],
            ),
            "  Call-graph observation oracle",
            "    workload_cases={cases} observed={observed} confirmed={confirmed}; "
            "missed={missed} unconfirmed={unconfirmed}".format(
                cases=call_graph["n_workload_cases"],
                observed=call_graph["n_observed_mapped"],
                confirmed=call_graph["confirmed"],
                missed=residuals["call_graph_missed"],
                unconfirmed=residuals["call_graph_unconfirmed"],
            ),
            "  cJSON API surface",
            "    functions={functions} covered={covered}; ownership_blocked={blocked} "
            "global_state_excluded={global_state}".format(
                functions=surface["functions"],
                covered=surface["covered"],
                blocked=residuals["cjson_ownership_blocked"],
                global_state=residuals["cjson_global_state_excluded"],
            ),
            "  residuals (not silently counted as clean)",
            "    C liveness: {unknown} unknown, {vacuous} vacuous; "
            "registry: {registry} missed; call graph: {calls} missed, "
            "{unconfirmed} unconfirmed; cJSON: {blocked} ownership-blocked, "
            "{global_state} process-global exclusions".format(
                unknown=residuals["c_preprocessor_unknown"],
                vacuous=residuals["c_preprocessor_vacuous"],
                registry=residuals["python_registry_missed"],
                calls=residuals["call_graph_missed"],
                unconfirmed=residuals["call_graph_unconfirmed"],
                blocked=residuals["cjson_ownership_blocked"],
                global_state=residuals["cjson_global_state_excluded"],
            ),
            f"  RESULT: {'PASS' if report['ok'] else 'FAIL'}",
        ]
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit the native reports")
    args = parser.parse_args(argv)
    try:
        report = build_report()
    except OracleRunError as error:
        print(f"Independent-oracle summary: FAIL\n{error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(format_report(report))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
