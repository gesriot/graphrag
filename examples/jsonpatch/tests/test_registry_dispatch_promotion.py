"""Registry-dispatch promotion: static Name table members → non-det calls edges.

Promotion rule (scripts/python_dynamic.py::_iter_registry_dispatch_targets):
  1. Statically extracted registry entry (Name/Attribute value, concrete key)
  2. Dispatch site labelled registry_lookup / polymorphic_call / …
  3. Target entity already in the graph

Runtime confirmation justifies the policy; it is not a licence to promote
runtime-only or lambda-valued members.

Run: uv run python -m pytest examples/jsonpatch/tests/test_registry_dispatch_promotion.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from mini_game_to_byog import build_byog_for_package  # type: ignore
from python_dynamic import (  # type: ignore
    REGISTRY_DISPATCH_CONFIDENCE,
    analyze_source_text,
    annotate_byog,
)


def test_jsonpatch_promotes_six_apply_targets_non_deterministic():
    data = build_byog_for_package(package_dir=ROOT / "examples" / "jsonpatch")
    edges = [
        r
        for r in data["relationships"]
        if r.get("type") == "calls"
        and r.get("extractor") == "python_dynamic_registry"
        and r.get("source") == "jsonpatch:JsonPatch.apply"
    ]
    targets = {str(r.get("target")) for r in edges}
    for op in (
        "AddOperation",
        "RemoveOperation",
        "ReplaceOperation",
        "MoveOperation",
        "TestOperation",
        "CopyOperation",
    ):
        assert f"jsonpatch:{op}.apply" in targets, targets
    assert len(edges) == 6
    assert all(r.get("is_deterministic") is False for r in edges)
    assert all(float(r.get("confidence")) == REGISTRY_DISPATCH_CONFIDENCE for r in edges)


def test_lambda_registry_does_not_promote(tmp_path: Path):
    """Lambda-valued tables have no Name callee — never a calls edge."""
    (tmp_path / "m.py").write_text(
        "MAP = {'a': lambda x: x, 'b': lambda x: x + 1}\n"
        "def run(k, v):\n"
        "    return MAP[k](v)\n",
        encoding="utf-8",
    )
    data = build_byog_for_package(package_dir=tmp_path)
    promoted = [
        r
        for r in data["relationships"]
        if r.get("extractor") == "python_dynamic_registry"
    ]
    assert promoted == [], promoted
    # Table is still detected as a registry (sites), but empty Name entries.
    fa = analyze_source_text((tmp_path / "m.py").read_text(), tmp_path / "m.py")
    assert "MAP" in fa.registries
    assert fa.registry_tables.get("MAP") == []


def test_promotion_idempotent_on_reannotate():
    data = build_byog_for_package(package_dir=ROOT / "examples" / "jsonpatch")
    n1 = sum(
        1
        for r in data["relationships"]
        if r.get("extractor") == "python_dynamic_registry"
    )
    annotate_byog(data, ROOT / "examples" / "jsonpatch")
    annotate_byog(data, ROOT / "examples" / "jsonpatch")
    n2 = sum(
        1
        for r in data["relationships"]
        if r.get("extractor") == "python_dynamic_registry"
    )
    # 6 `.apply` targets + 6 class constructs from `_get_operation` + the
    # inherited constructor `PatchOperation.__init__`. The six operation classes
    # define no `__init__` of their own (verified: `AddOperation.__init__ is
    # PatchOperation.__init__`), so `cls(operation)` runs the base one — an MRO
    # fact from the same file, not a guess.
    assert n1 == n2 == 13
    inherited = [
        r
        for r in data["relationships"]
        if r.get("extractor") == "python_dynamic_registry"
        and str(r.get("target")).endswith("PatchOperation.__init__")
    ]
    assert len(inherited) == 1, inherited
    assert inherited[0]["is_deterministic"] is False


def test_no_promote_without_dispatch_site_label(tmp_path: Path):
    """A registry table alone (never looked up) does not mint edges."""
    (tmp_path / "m.py").write_text(
        "class A: pass\n"
        "class B: pass\n"
        "class Reg:\n"
        "    OPS = {'a': A, 'b': B}\n"
        "    def unused(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    data = build_byog_for_package(package_dir=tmp_path)
    promoted = [
        r
        for r in data["relationships"]
        if r.get("extractor") == "python_dynamic_registry"
    ]
    assert promoted == [], promoted
