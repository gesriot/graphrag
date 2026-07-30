"""The combined oracle entry point must report the native tool outputs verbatim."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from oracle_summary import build_report, format_report  # type: ignore


def _run_json(script: Path, *args: str) -> dict[str, object]:
    proc = subprocess.run(
        [sys.executable, str(script), *args, "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    data = json.loads(proc.stdout)
    assert isinstance(data, dict)
    return data


@pytest.mark.skipif(
    shutil.which("clang") is None and shutil.which("cc") is None and shutil.which("gcc") is None,
    reason="combined oracle requires a C compiler for the liveness comparator",
)
def test_combined_oracle_summary_matches_each_native_oracle():
    """Do not replace a child report with a copied or hand-maintained number."""
    combined = build_report()
    oracles = combined["oracles"]
    residuals = combined["residuals"]

    liveness = _run_json(
        ROOT / "scripts" / "c_preprocessor.py",
        "--package",
        "examples/cjson",
        "--vs-compiler",
    )
    registry = _run_json(
        ROOT / "scripts" / "python_dynamic.py",
        "--package",
        "examples/isodate",
        "--vs-runtime",
    )
    calls = _run_json(
        ROOT / "scripts" / "call_graph_oracle.py",
        "--package",
        "jsonpatch",
    )

    assert oracles["c_preprocessor_liveness"]["regions_vacuous"] == liveness[
        "regions_vacuous"
    ]
    assert oracles["python_registry"]["missed"] == registry["missed"]
    assert oracles["call_graph_observation"]["missed"] == calls["missed"]
    assert oracles["call_graph_observation"]["unconfirmed"] == calls["unconfirmed"]
    assert residuals["c_preprocessor_vacuous"] == liveness["regions_vacuous"]
    assert residuals["python_registry_missed"] == registry["missed"]
    assert residuals["call_graph_missed"] == calls["missed"]
    assert residuals["call_graph_unconfirmed"] == calls["unconfirmed"]

    audit = subprocess.run(
        [
            sys.executable,
            str(ROOT / "examples" / "cjson" / "tools" / "api_surface_audit.py"),
            "--check",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert audit.returncode == 0, audit.stderr or audit.stdout
    surface = oracles["cjson_api_surface"]
    assert residuals["cjson_ownership_blocked"] == surface["ownership_blocked"]
    assert residuals["cjson_global_state_excluded"] == surface["global_state_excluded"]
    assert "native categories are intentionally separate" in format_report(combined)
