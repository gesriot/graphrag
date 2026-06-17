"""Golden-master behavior contract for the v1 (diff) scope of diff-match-patch.

Golden files live in tests/diff/. Each case pins one op against the reference:
- diff:              (text1, text2, checklines) -> exact diff ops  [Diff_Timeout=0]
- cleanup_semantic:  input ops -> expected ops
- cleanup_efficiency:input ops -> expected ops                    [Diff_EditCost=4]
- cleanup_merge:     input ops -> expected ops

Ops are exact (they pin diff-match-patch 20241021), and raw diffs additionally
satisfy the reconstruction/op-code invariants in behavior_contract.json.

Run:
    uv run python -m pytest examples/diff_match_patch/tests/test_diff_contract.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parents[2]
sys.path.insert(0, str(EXAMPLES_DIR))

import diff_match_patch as dmp_mod  # type: ignore  # noqa: E402

DIFF_DIR = Path(__file__).parent / "diff"
GOLDEN_FILES = sorted(DIFF_DIR.glob("golden_*.json"))

DELETE, EQUAL, INSERT = -1, 0, 1


def _all_cases():
    cases = []
    for gf in GOLDEN_FILES:
        data = json.loads(gf.read_text())
        for c in data["cases"]:
            cases.append(pytest.param(c, id=f"{c['op']}:{gf.stem}:{data['cases'].index(c)}"))
    return cases


def test_golden_files_present_and_sized():
    assert GOLDEN_FILES, "no golden_*.json files under tests/diff/"
    total = sum(len(json.loads(gf.read_text())["cases"]) for gf in GOLDEN_FILES)
    assert total >= 35, f"expected >= 35 diff golden cases, got {total}"


def _ops(pairs):
    return [(op, txt) for op, txt in pairs]


def _check_invariants(diffs, text1, text2):
    assert all(op in (-1, 0, 1) for op, _ in diffs), diffs
    assert "".join(t for op, t in diffs if op in (EQUAL, DELETE)) == text1
    assert "".join(t for op, t in diffs if op in (EQUAL, INSERT)) == text2


def _reconstructed_texts(diffs):
    return (
        "".join(t for op, t in diffs if op in (EQUAL, DELETE)),
        "".join(t for op, t in diffs if op in (EQUAL, INSERT)),
    )


def _check_merge_shape(diffs):
    assert all(text or (index == 0 and op == EQUAL) for index, (op, text) in enumerate(diffs))
    assert all(
        left_op != right_op or left_op == EQUAL
        for (left_op, _), (right_op, _) in zip(diffs, diffs[1:])
    )


def _run_case(case):
    op = case["op"]
    if op == "diff":
        d = dmp_mod.diff_match_patch()
        d.Diff_Timeout = 0
        diffs = d.diff_main(case["text1"], case["text2"], case["checklines"])
        assert [[o, t] for o, t in diffs] == case["diffs"]
        _check_invariants(diffs, case["text1"], case["text2"])
    elif op in ("cleanup_semantic", "cleanup_efficiency", "cleanup_merge"):
        d = dmp_mod.diff_match_patch()
        diffs = _ops(case["input"])
        before_texts = _reconstructed_texts(diffs)
        if op == "cleanup_semantic":
            d.diff_cleanupSemantic(diffs)
        elif op == "cleanup_efficiency":
            d.Diff_EditCost = 4
            d.diff_cleanupEfficiency(diffs)
        else:
            d.diff_cleanupMerge(diffs)
        assert [[o, t] for o, t in diffs] == case["expected"]
        assert _reconstructed_texts(diffs) == before_texts
        if op == "cleanup_merge":
            _check_merge_shape(diffs)
    else:  # pragma: no cover
        raise AssertionError(f"unknown op {op!r}")


@pytest.mark.parametrize("case", _all_cases())
def test_golden_case(case):
    _run_case(case)


def test_behavior_contract_config():
    contract = json.loads((DIFF_DIR / "behavior_contract.json").read_text())
    assert contract["config"]["Diff_Timeout"] == 0
    assert contract["config"]["Diff_EditCost"] == 4
