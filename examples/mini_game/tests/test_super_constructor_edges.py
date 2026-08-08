"""Regression coverage for same-file ``super().__init__`` call candidates."""
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from extract_python import extract_from_file  # type: ignore


def test_zero_argument_super_init_records_defining_class_mro_candidate(tmp_path: Path):
    """The lexical candidate is narrow but not a runtime-exact dispatch fact."""
    source = tmp_path / "m.py"
    source.write_text(
        "class Root:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n"
        "class Left(Root):\n"
        "    def __init__(self):\n"
        "        super().__init__()\n"
        "\n"
        "class Right(Root):\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n"
        "class Child(Left, Right):\n"
        "    def __init__(self):\n"
        "        super().__init__()\n",
        encoding="utf-8",
    )

    result = extract_from_file(source)
    calls = {
        (str(rel["source"]), str(rel["target"])): rel
        for rel in result["relationships"]
        if rel.get("type") == "calls"
    }

    assert ("m:Left.__init__", "m:Root.__init__") in calls
    assert ("m:Child.__init__", "m:Left.__init__") in calls
    assert ("m:Child.__init__", "m:Root.__init__") not in calls
    # Executing Left.__init__ on a Child instance continues to Right, not Root;
    # therefore the defining-class MRO candidate must never claim certainty.
    assert calls[("m:Left.__init__", "m:Root.__init__")]["is_deterministic"] is False
    assert calls[("m:Left.__init__", "m:Root.__init__")]["confidence"] == 0.75
