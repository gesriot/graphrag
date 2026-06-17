"""Golden-master behavior contract for the v2 (match / Bitap) scope of diff-match-patch.

Golden files live in tests/match/. Each case pins:
    (text, pattern, loc) with (Match_Threshold, Match_Distance) -> best match index or -1

Notably includes patterns longer than 64/128 characters: Python's Bitap uses
arbitrary-precision ints, so the Rust port must NOT use a fixed-width integer for
the bit arrays.

Run:
    uv run python -m pytest examples/diff_match_patch/tests/test_match_contract.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parents[2]
sys.path.insert(0, str(EXAMPLES_DIR))

import diff_match_patch as dmp_mod  # type: ignore  # noqa: E402

MATCH_DIR = Path(__file__).parent / "match"
GOLDEN_FILES = sorted(MATCH_DIR.glob("golden_*.json"))


def _all_cases():
    cases = []
    for gf in GOLDEN_FILES:
        data = json.loads(gf.read_text())
        for c in data["cases"]:
            cases.append(pytest.param(c, id=f"{c['op']}:{c['pattern']!r}@{c['loc']}"))
    return cases


def test_golden_files_present_and_sized():
    assert GOLDEN_FILES, "no golden_*.json files under tests/match/"
    total = sum(len(json.loads(gf.read_text())["cases"]) for gf in GOLDEN_FILES)
    assert total >= 25, f"expected >= 25 match golden cases, got {total}"


@pytest.mark.parametrize("case", _all_cases())
def test_golden_case(case):
    assert case["op"] == "match"
    d = dmp_mod.diff_match_patch()
    d.Match_Threshold = case["match_threshold"]
    d.Match_Distance = case["match_distance"]
    assert d.match_main(case["text"], case["pattern"], case["loc"]) == case["result"]
