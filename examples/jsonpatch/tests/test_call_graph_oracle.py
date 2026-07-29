"""Call-graph oracle: published edges vs sys.setprofile under golden contracts.

Measurement only — does not modify graphs, is_deterministic, or audit pass rates.

Run: uv run python -m pytest examples/jsonpatch/tests/test_call_graph_oracle.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from call_graph_oracle import (  # type: ignore
    compare_call_edges_to_trace,
    compare_named_package,
    format_call_oracle_report,
    load_published_call_edges,
    score_call_edges,
    known_contracts,
)


def test_loads_published_edges_not_fresh_extract():
    """Oracle must read the snapshot the ports were built on."""
    pub = load_published_call_edges(ROOT / "byog_jsonpatch")
    assert pub["n_call_rows"] == 104, pub
    assert pub["n_calls"] == 84  # unique directed pairs
    assert pub["snapshot"]
    # Sanity: apply_patch edge exists on the published graph.
    assert ("jsonpatch:apply_patch", "jsonpatch:JsonPatch") in pub["edges"]


def test_jsonpatch_oracle_runs_and_separates_directions():
    report = compare_named_package("jsonpatch")
    assert report["status"] == "ok", report.get("skip_reason")
    assert report["n_graph_call_rows"] == 104
    assert report["n_graph_calls"] == 84
    assert report["n_observed_mapped"] >= 10
    # Both directions reported; neither is folded into a single agreement score.
    assert "confirmed" in report and "missed" in report and "unconfirmed" in report
    assert report["confirmed"] + report["missed"] == report["n_observed_mapped"]
    assert report["confirmed"] + report["unconfirmed"] == report["n_graph_calls"]
    # Dynamic dispatch under apply is expected among misses.
    missed_pairs = {
        (e["caller"], e["callee"]) for e in report["missed_edges"]
    }
    assert any(
        c == "jsonpatch:JsonPatch.apply" and "Operation.apply" in t
        for c, t in missed_pairs
    ), missed_pairs
    # Unconfirmed includes code the apply golden never reaches (e.g. DiffBuilder).
    unc = {(e["caller"], e["callee"]) for e in report["unconfirmed_edges"]}
    assert any("DiffBuilder" in a or "DiffBuilder" in b for a, b in unc), unc
    text = format_call_oracle_report(report)
    assert "unconfirmed" in text
    assert "missed" in text


def test_mini_lang_oracle_has_high_observed_recall():
    report = compare_named_package("mini_lang")
    assert report["status"] == "ok", report.get("skip_reason")
    assert report["n_graph_call_rows"] == 69
    assert report["confirmed"] >= 25
    # Most observed package calls should already be edges on this small graph.
    assert report["recall_of_observed"] is not None
    assert report["recall_of_observed"] >= 0.75
    assert report["missed"] >= 0  # may be small but never hidden


def test_deleted_edge_shows_as_missed_fabricated_as_unconfirmed():
    """Plant both directions without rewriting the snapshot."""
    full = compare_call_edges_to_trace(
        ROOT / "byog_mini_lang",
        ROOT / "examples" / "mini_lang",
        workload="mini_lang_golden",
    )
    assert full["status"] == "ok", full.get("skip_reason")
    assert full["confirmed_edges"], full
    pub = load_published_call_edges(ROOT / "byog_mini_lang")
    observed = {
        (e["caller"], e["callee"])
        for e in (full["confirmed_edges"] + full["missed_edges"])
    }
    # Full score lists are capped at 80; rebuild observed from the live counts
    # by re-tracing is already done — use score inputs from confirmed∪missed
    # samples only when small enough. Prefer exact sets via a second score on
    # published edges vs the observed reconstructed from the uncapped formula:
    # confirmed + missed == n_observed_mapped when samples cover all (mini_lang).
    assert len(observed) == full["n_observed_mapped"], (
        len(observed),
        full["n_observed_mapped"],
    )
    edges = set(pub["edges"])
    scored_base = score_call_edges(edges, observed)
    assert scored_base["confirmed"] == full["confirmed"]
    assert scored_base["missed"] == full["missed"]

    victim = (full["confirmed_edges"][0]["caller"], full["confirmed_edges"][0]["callee"])
    deleted = set(edges)
    deleted.discard(victim)
    scored_del = score_call_edges(deleted, observed)
    assert scored_del["missed"] == scored_base["missed"] + 1
    assert scored_del["confirmed"] == scored_base["confirmed"] - 1
    assert any(
        e["caller"] == victim[0] and e["callee"] == victim[1]
        for e in scored_del["missed_edges"]
    )

    fake = ("eval:FakeCaller.method", "eval:FakeCallee.method")
    assert fake not in observed and fake not in edges
    fabricated = set(edges) | {fake}
    scored_fab = score_call_edges(fabricated, observed)
    assert scored_fab["unconfirmed"] == scored_base["unconfirmed"] + 1
    assert any(
        e["caller"] == fake[0] and e["callee"] == fake[1]
        for e in scored_fab["unconfirmed_edges"]
    )
    text = format_call_oracle_report({**full, **scored_fab, "status": "ok"})
    assert "not a defect" in text


def test_unconfirmed_is_not_a_failure_status():
    report = compare_named_package("jsonpatch")
    # Even with dozens of unconfirmed edges, status stays ok — coverage, not error.
    assert report["unconfirmed"] > 0
    assert report["status"] == "ok"
    assert report["ok"] is True


def test_cli_json_smoke():
    import subprocess

    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "call_graph_oracle.py"),
            "-p",
            "mini_lang",
            "--json",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["confirmed"] >= 25
    assert "missed" in data and "unconfirmed" in data


def test_known_contracts_point_at_published_graphs():
    for name, c in known_contracts().items():
        assert c.package_dir.is_dir(), name
        assert (c.graph_dir / "current").is_file() or (
            c.graph_dir / "entities.parquet"
        ).is_file(), name


def test_workload_reports_the_corpus_it_actually_executed():
    """A workload that runs a substitute corpus must not read as a clean run.

    `run_humanize_number` looked for the golden at `tests/golden_number.json`
    while it lives at `tests/number/golden_number.json`, so it silently fell
    back to an eight-call hand-written smoke set — and nothing in the report
    said which corpus produced the numbers. It also read `fn`/`function`/`name`
    while the golden uses `func`, so even a found golden would have executed
    zero cases.
    """
    expected = {
        "jsonpatch": ("golden_apply.json", 25),
        "mini_lang": ("golden_arithmetic.json", 12),
        "humanize": ("golden_number.json", 59),
    }
    for pkg, (golden_name, at_least) in expected.items():
        report = compare_named_package(pkg)
        assert report["status"] == "ok", (pkg, report.get("skip_reason"))
        assert report["n_workload_cases"] > 0, (pkg, report)
        files = dict(report["workload_cases"])
        assert golden_name in files, (pkg, report["workload_cases"])
        assert files[golden_name] >= at_least, (pkg, report["workload_cases"])


def test_missing_golden_refuses_rather_than_substituting(tmp_path: Path):
    """Hiding the corpus must surface as a skip with a reason, not a smoke set."""
    import shutil

    staged = tmp_path / "humanize"
    shutil.copytree(ROOT / "examples" / "humanize", staged)
    for golden in staged.rglob("golden_number.json"):
        golden.unlink()
    report = compare_call_edges_to_trace(
        ROOT / "byog_humanize", staged, workload="humanize_number"
    )
    assert report["status"] == "skipped", report
    assert "refusing to substitute" in str(report.get("skip_reason")), report
