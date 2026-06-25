"""Golden-master behavior contract for the `sqlparse.split` pipeline (Phase 5
scaled component port target).

Golden files live in tests/split/. Each case pins:
    split(sql, strip_semicolon=...) -> list of statement strings

`split` exercises a real cross-module pipeline: FilterStack -> lexer.tokenize
(SQL_REGEX) -> StatementSplitter -> optional semicolon stripping -> str(Statement).
Notably, semicolons inside string literals / line comments / block comments are
NOT statement separators -- the Rust port must reproduce that exactly.

Run:
    uv run python -m pytest examples/sqlparse/tests/test_split_contract.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parents[2]
sys.path.insert(0, str(EXAMPLES_DIR))

import sqlparse  # type: ignore  # noqa: E402

SPLIT_DIR = Path(__file__).parent / "split"
GOLDEN_FILES = sorted(SPLIT_DIR.glob("golden_*.json"))


def _all_cases():
    cases = []
    for gf in GOLDEN_FILES:
        data = json.loads(gf.read_text())
        for c in data["cases"]:
            cases.append(pytest.param(c, id=f"{c['op']}:{data['cases'].index(c)}"))
    return cases


def test_golden_files_present_and_sized():
    assert GOLDEN_FILES, "no golden_*.json files under tests/split/"
    total = sum(len(json.loads(gf.read_text())["cases"]) for gf in GOLDEN_FILES)
    assert total >= 12, f"expected >= 12 split golden cases, got {total}"


@pytest.mark.parametrize("case", _all_cases())
def test_golden_case(case):
    assert case["op"] == "split"
    got = sqlparse.split(case["sql"], strip_semicolon=case["strip_semicolon"])
    assert got == case["result"]
