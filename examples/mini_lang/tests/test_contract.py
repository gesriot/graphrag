"""Golden-master behavior contract for mini_lang.

Each golden_*.json case pins `source -> (stdout, error)`. Any port (Rust, etc.)
must reproduce these exactly. This is the second Python->Rust porting target's
equivalent of mini_game's golden traces.

Run:
    PYTHONPATH=examples/mini_lang uv run python -m pytest examples/mini_lang/tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PKG_DIR))

from main import run_source  # type: ignore  # noqa: E402

GOLDEN_FILES = sorted(PKG_DIR.glob("tests/golden_*.json"))


def _all_cases():
    cases = []
    for gf in GOLDEN_FILES:
        data = json.loads(gf.read_text())
        for c in data["cases"]:
            cases.append(pytest.param(c, id=f"{data['name']}:{c['source']!r}"))
    return cases


def test_golden_files_present_and_sized():
    assert GOLDEN_FILES, "no golden_*.json files found"
    total = sum(len(json.loads(gf.read_text())["cases"]) for gf in GOLDEN_FILES)
    assert total >= 20, f"expected >= 20 golden cases, got {total}"


@pytest.mark.parametrize("case", _all_cases())
def test_golden_case(case):
    outputs, error = run_source(case["source"])
    assert outputs == case["stdout"], f"stdout mismatch for {case['source']!r}"
    assert error == case["error"], f"error mismatch for {case['source']!r}"
