"""Same-file inherited-member edges are precise closure evidence, not expansion.

The edge reaches a base declaration only when a subclass has no declaration of
that member.  That lets an adequacy closure enter inherited properties without
pretending that an override calls its base implementation.

Run: uv run python -m pytest examples/jsonpatch/tests/test_inherited_member_edges.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from mini_game_to_byog import build_byog_for_package  # type: ignore


def _inherited_pairs(data: dict) -> set[tuple[str, str]]:
    return {
        (str(rel["source"]), str(rel["target"]))
        for rel in data["relationships"]
        if rel.get("type") == "inherits"
    }


def test_unoverridden_members_are_inherited_but_override_suppresses_the_edge(
    tmp_path: Path,
):
    """Planting an override must remove exactly that inherited-member fact."""
    (tmp_path / "m.py").write_text(
        "class Base:\n"
        "    def inherited(self):\n"
        "        return 1\n"
        "    def overridden(self):\n"
        "        return 2\n"
        "    @property\n"
        "    def token(self):\n"
        "        return 3\n"
        "\n"
        "class Child(Base):\n"
        "    pass\n"
        "\n"
        "class Override(Base):\n"
        "    def overridden(self):\n"
        "        return 4\n"
        "    @property\n"
        "    def token(self):\n"
        "        return 5\n",
        encoding="utf-8",
    )
    data = build_byog_for_package(package_dir=tmp_path)
    pairs = _inherited_pairs(data)

    assert {
        ("m:Child", "m:Base.inherited"),
        ("m:Child", "m:Base.overridden"),
        ("m:Child", "m:Base.token"),
        ("m:Override", "m:Base.inherited"),
    } <= pairs
    assert ("m:Override", "m:Base.overridden") not in pairs
    assert ("m:Override", "m:Base.token") not in pairs

    inherited = next(
        rel
        for rel in data["relationships"]
        if rel.get("source") == "m:Child" and rel.get("target") == "m:Base.inherited"
    )
    assert inherited["is_deterministic"] is True
    assert inherited["confidence"] == 0.95
    assert "not a call" in str(inherited["description"])


def test_jsonpatch_properties_are_inherited_but_abstract_apply_is_not():
    """All operation classes override ``apply``; no base-apply edge is honest."""
    data = build_byog_for_package(package_dir=ROOT / "examples" / "jsonpatch")
    pairs = _inherited_pairs(data)
    operations = (
        "AddOperation",
        "RemoveOperation",
        "ReplaceOperation",
        "MoveOperation",
        "TestOperation",
        "CopyOperation",
    )
    for operation in operations:
        source = f"jsonpatch:{operation}"
        assert (source, "jsonpatch:PatchOperation.path") in pairs
        assert (source, "jsonpatch:PatchOperation.key") in pairs
        assert (source, "jsonpatch:PatchOperation.apply") not in pairs

    spec = json.loads(
        (ROOT / "scripts" / "ablation_specs" / "jsonpatch_adequacy.json").read_text()
    )
    assert "jsonpatch:PatchOperation.apply" not in spec["must_reach"]
    residual = spec["_intentionally_unreached"]["jsonpatch:PatchOperation.apply"]
    assert "six registry operation classes" in residual
