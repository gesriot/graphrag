"""Golden-master behavior contract for the v3 (patch) scope of diff-match-patch.

Golden files live in tests/patch/. Each case pins one op:
- make:      (text1, text2)        -> patch_toText(patch_make(...))
- apply:     (patch_text, source)  -> (result_text, [bool, ...])
- roundtrip: patch_text            -> patch_toText(patch_fromText(...)) (byte-for-byte)
- invalid:   patch_text            -> ValueError message

The Rust patch port (v3) must reproduce these exactly, including the percent
encoding of patch bodies and fuzzy application against imperfect sources.

Run:
    uv run python -m pytest examples/diff_match_patch/tests/test_patch_contract.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parents[2]
sys.path.insert(0, str(EXAMPLES_DIR))

import diff_match_patch as dmp_mod  # type: ignore  # noqa: E402

PATCH_DIR = Path(__file__).parent / "patch"
GOLDEN_FILES = sorted(PATCH_DIR.glob("golden_*.json"))


def _dmp():
    d = dmp_mod.diff_match_patch()
    d.Diff_Timeout = 0
    return d


def _all_cases():
    cases = []
    for gf in GOLDEN_FILES:
        data = json.loads(gf.read_text())
        for c in data["cases"]:
            cases.append(pytest.param(c, id=f"{c['op']}:{data['cases'].index(c)}"))
    return cases


def test_golden_files_present_and_sized():
    assert GOLDEN_FILES, "no golden_*.json files under tests/patch/"
    total = sum(len(json.loads(gf.read_text())["cases"]) for gf in GOLDEN_FILES)
    assert total >= 20, f"expected >= 20 patch golden cases, got {total}"


@pytest.mark.parametrize("case", _all_cases())
def test_golden_case(case):
    d = _dmp()
    op = case["op"]
    if op == "make":
        patches = d.patch_make(case["text1"], case["text2"])
        assert d.patch_toText(patches) == case["patch_text"]
    elif op == "apply":
        patches = d.patch_fromText(case["patch_text"])
        patches_before = d.patch_toText(patches)
        new_text, results = d.patch_apply(patches, case["source"])
        assert new_text == case["result"]
        assert results == case["results"]
        assert d.patch_toText(patches) == patches_before
        # Re-applying the same patch objects must remain stable as well.
        new_text2, results2 = d.patch_apply(patches, case["source"])
        assert (new_text2, results2) == (new_text, results)
        assert d.patch_toText(patches) == patches_before
    elif op == "roundtrip":
        assert d.patch_toText(d.patch_fromText(case["patch_text"])) == case["expected"]
    elif op == "invalid":
        with pytest.raises(ValueError) as exc:
            d.patch_fromText(case["patch_text"])
        assert str(exc.value) == case["error"]
    else:  # pragma: no cover
        raise AssertionError(f"unknown op {op!r}")
