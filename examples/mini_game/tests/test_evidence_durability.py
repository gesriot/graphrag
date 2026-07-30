"""Regression checks for the manifest-derived local-evidence inventory.

The inventory exists because a hand-written exception list omitted jsonpatch.
These tests make both declarative inputs move the inventory, and make the
already-lost SQLParse snapshot a structured record rather than a note parser.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from evidence_durability import build_inventory, render_markdown  # type: ignore


def _artifacts(report: dict[str, object]) -> dict[str, dict[str, object]]:
    rows = report["artifacts"]
    assert isinstance(rows, list)
    return {
        str(row["artifact"]): row
        for row in rows
        if isinstance(row, dict)
    }


def test_inventory_derives_frozen_lost_and_oracle_consumers():
    report = build_inventory()
    artifacts = _artifacts(report)

    sqlparse_uses = artifacts["byog_sqlparse"]["claim_uses"]
    assert isinstance(sqlparse_uses, list)
    assert {
        (use["claim"], use["tier"], use["snapshot"])
        for use in sqlparse_uses
    } == {
        ("sqlparse_graph_current_index", "local-frozen-snapshot", "20260625-154143-8ce62d57"),
        ("sqlparse_graph_phase5_baseline", "historical-record", "20260618-151436-ad7b5954"),
    }
    assert artifacts["byog_isodate"]["claim_uses"][0]["tier"] == "local-frozen-snapshot"
    assert artifacts["byog_jsonpatch"]["oracle_uses"] == ["jsonpatch"]
    jsonpatch_claim = artifacts["byog_jsonpatch"]["claim_uses"][0]
    assert jsonpatch_claim["claim"] == "oracle_residuals"
    assert jsonpatch_claim["tier"] == "local-published-oracle-input"
    assert jsonpatch_claim["replay"] == (
        "not reproducible from Git; reindex changes the call-oracle baseline"
    )
    assert jsonpatch_claim["present"] is (ROOT / "byog_jsonpatch").is_dir()

    # byog_graph is the implementation module, not an ignored artifact.  A
    # Markdown search that cannot tell those apart creates a phantom risk row.
    assert "byog_graph" not in artifacts


def test_inventory_moves_when_claim_manifest_gains_a_local_snapshot(tmp_path: Path):
    manifest = json.loads((ROOT / "scripts" / "doc_claims.json").read_text())
    manifest["claims"].append(
        {
            "id": "durability_manifest_probe",
            "kind": "frozen_snapshot",
            "source": {
                "type": "graph_counts",
                "mode": "live",
                "graph": "byog_manifest_probe",
                "snapshot": "20260730-000000-deadbeef",
            },
            "expect": {},
            "docs": [],
        }
    )
    path = tmp_path / "doc_claims.json"
    path.write_text(json.dumps(manifest))

    artifacts = _artifacts(build_inventory(claims_manifest=path))
    uses = artifacts["byog_manifest_probe"]["claim_uses"]
    assert uses == [
        {
            "claim": "durability_manifest_probe",
            "tier": "local-frozen-snapshot",
            "replay": "not reproducible from Git",
            "artifact": "byog_manifest_probe",
            "snapshot": "20260730-000000-deadbeef",
            "present": False,
            "detail": "Named snapshot is locally protected from retention, not versioned.",
            "replay_probe": None,
        }
    ]


def test_inventory_moves_when_gate_manifest_gains_a_declared_gap(tmp_path: Path):
    gates = json.loads((ROOT / "scripts" / "port_gates.json").read_text())
    gates["ports"].append(
        {
            "id": "durability_gate_probe",
            "kind": "gap",
            "gap": "test-only manifest coverage probe",
        }
    )
    path = tmp_path / "port_gates.json"
    path.write_text(json.dumps(gates))

    report = build_inventory(gates_manifest=path)
    rows = {row["id"]: row for row in report["gate_outputs"]}
    assert rows["durability_gate_probe"] == {
        "id": "durability_gate_probe",
        "kind": "gap",
        "output": None,
        "replay": "no Rust-port gate; named source-only gap",
    }


def test_checked_in_reader_view_matches_the_derived_inventory():
    expected = render_markdown(build_inventory())
    actual = (ROOT / "docs" / "EVIDENCE_DURABILITY.md").read_text()
    assert actual == expected
