"""Runtime oracle tests for same-file inherited-member extractor facts."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from inherited_member_runtime_audit import (  # type: ignore
    build_report,
    inherited_member_candidates,
    verify_against_runtime,
)


def _package(tmp_path: Path, name: str, module_source: str) -> Path:
    package = tmp_path / name
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "m.py").write_text(module_source, encoding="utf-8")
    return package


def test_runtime_audit_confirms_c3_properties_and_slots(tmp_path: Path):
    """The oracle sees the same owner as Python for nontrivial local C3 MRO."""
    package = _package(
        tmp_path,
        "runtime_c3",
        "class Base:\n"
        "    def method(self):\n"
        "        return 'base'\n"
        "    def only_base(self):\n"
        "        return 'only-base'\n"
        "    @property\n"
        "    def token(self):\n"
        "        return 'token'\n"
        "\n"
        "class Left(Base):\n"
        "    def method(self):\n"
        "        return super().method()\n"
        "\n"
        "class Right(Base):\n"
        "    pass\n"
        "\n"
        "class Leaf(Left, Right):\n"
        "    pass\n"
        "\n"
        "class Slotted(Base):\n"
        "    __slots__ = ()\n",
    )
    candidates = inherited_member_candidates(package)
    pairs = {(row["source"], row["target"]) for row in candidates}
    assert ("m:Leaf", "m:Left.method") in pairs
    assert ("m:Leaf", "m:Base.only_base") in pairs
    assert ("m:Leaf", "m:Base.token") in pairs
    assert ("m:Slotted", "m:Base.method") in pairs
    assert ("m:Leaf", "m:Base.method") not in pairs
    assert ("m:Left", "m:Base.method") not in pairs
    assert ("m:Left", "m:Base.only_base") in pairs

    rows = verify_against_runtime(package, candidates)
    assert rows
    assert {row["status"] for row in rows} == {"confirmed"}


def test_runtime_audit_reports_class_assignment_shadowing(tmp_path: Path):
    """A non-FunctionDef class override must surface as a false edge, not pass."""
    package = _package(
        tmp_path,
        "runtime_shadow",
        "class Base:\n"
        "    def method(self):\n"
        "        return 'base'\n"
        "\n"
        "class Child(Base):\n"
        "    method = 'class-level override'\n",
    )
    report = build_report((str(package),))
    assert report["ok"] is False
    assert report["totals"] == {
        "candidates": 1,
        "confirmed": 0,
        "mismatches": 1,
        "errors": 0,
    }
    row = report["packages"][0]["mismatch_rows"][0]
    assert row["source"] == "m:Child"
    assert row["target"] == "m:Base.method"
    assert row["reason"] == "runtime_member_owner_differs"


def test_runtime_audit_does_not_call_an_empty_population_a_pass(tmp_path: Path):
    """No emitted member facts is an explicit non-measurement, not 0/0 correct."""
    package = _package(tmp_path, "runtime_empty", "class Only:\n    pass\n")
    report = build_report((str(package),))
    assert report["totals"] == {
        "candidates": 0,
        "confirmed": 0,
        "mismatches": 0,
        "errors": 0,
    }
    assert report["error_rate"] is None
    assert report["ok"] is False
