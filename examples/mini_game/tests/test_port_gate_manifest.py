"""The port-evidence manifest must fail closed in both directions.

Rust ports are discovered from ``examples/*_rust/Cargo.toml`` and cross-checked,
so one cannot be dropped silently. Source packages carrying a golden contract
need the same treatment: without it a target vanishes from the report instead of
being listed with its gap. ``jsonpatch`` — 25 golden cases and a published
``byog_jsonpatch`` graph, no Rust port — was omitted exactly that way.

Run: uv run python -m pytest examples/mini_game/tests/test_port_gate_manifest.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from port_eval import (  # type: ignore
    load_aggregate_checks,
    load_gate_manifest as load_port_gates,
)

MANIFEST = ROOT / "scripts" / "port_gates.json"


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "port_gates.json"
    path.write_text(json.dumps(data, indent=2))
    return path


def test_shipped_manifest_loads():
    gates = load_port_gates(MANIFEST)
    assert gates, "manifest must declare gates"
    assert all("published_graph" in gate for gate in gates.values())
    aggregate = load_aggregate_checks(MANIFEST)
    assert [(check["name"], check.get("when")) for check in aggregate] == [
        ("published mutable-graph health", "full")
    ]


def test_every_rust_port_has_a_profile():
    gates = load_port_gates(MANIFEST)
    declared = {
        entry["port"] for entry in gates.values() if entry.get("kind", "port") == "port"
    }
    on_disk = {
        str(cargo.parent.relative_to(ROOT))
        for cargo in (ROOT / "examples").glob("*_rust/Cargo.toml")
    }
    assert declared == on_disk, (sorted(declared ^ on_disk),)


def test_every_contract_source_is_declared_port_or_gap():
    """A package with goldens is either gated or named as a gap — never absent."""
    gates = load_port_gates(MANIFEST)
    covered = {
        Path(entry["source"]).name if entry.get("source") else ident
        for ident, entry in gates.items()
    }
    with_contracts = {
        pkg.name
        for pkg in (ROOT / "examples").iterdir()
        if pkg.is_dir()
        and not pkg.name.endswith("_rust")
        and not pkg.name.startswith(".")
        and any((pkg / "tests").rglob("golden*"))
    }
    assert with_contracts <= covered, sorted(with_contracts - covered)
    # jsonpatch is the case that motivated the check; keep it pinned.
    assert "jsonpatch" in covered
    assert gates["jsonpatch"].get("kind") == "gap"


def test_dropping_a_contract_source_is_rejected(tmp_path: Path):
    data = json.loads(MANIFEST.read_text())
    data["ports"] = [p for p in data["ports"] if p.get("id") != "jsonpatch"]
    data["required_source_gaps"] = [
        g for g in data["required_source_gaps"] if g != "jsonpatch"
    ]
    with pytest.raises(ValueError, match="golden contract and no profile"):
        load_port_gates(_write(tmp_path, data))


def test_dropping_a_rust_port_is_rejected(tmp_path: Path):
    data = json.loads(MANIFEST.read_text())
    data["ports"] = [p for p in data["ports"] if p.get("id") != "jsmn"]
    with pytest.raises(ValueError, match="Rust-port coverage mismatch"):
        load_port_gates(_write(tmp_path, data))


def test_a_gap_must_name_its_gap(tmp_path: Path):
    data = json.loads(MANIFEST.read_text())
    for entry in data["ports"]:
        if entry.get("id") == "jsonpatch":
            entry["gap"] = ""
    with pytest.raises(ValueError, match="need a named gap"):
        load_port_gates(_write(tmp_path, data))
