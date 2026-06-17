"""Golden-master behavior contract for the vendored `semantic_version.SimpleSpec`
(v2a scope: SimpleSpec range matching only; NpmSpec is v2b).

Golden files live in tests/spec/ (separate from the Version golden in tests/) so
the Version contract test's op-dispatch is unaffected. Each case pins one op:
- match:   (spec, version)  -> bool
- invalid: spec             -> ValueError message
- select:  (spec, versions) -> best matching version str or None
- filter:  (spec, versions) -> list of matching version strs (in order)

The Rust SimpleSpec port (v2a) must reproduce these exactly.

Run:
    uv run python -m pytest examples/semantic_version/tests/test_simplespec_contract.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parents[2]
sys.path.insert(0, str(EXAMPLES_DIR))

import semantic_version as sv  # type: ignore  # noqa: E402

SPEC_DIR = Path(__file__).parent / "spec"
GOLDEN_FILES = sorted(SPEC_DIR.glob("golden_*.json"))


def _all_cases():
    cases = []
    for gf in GOLDEN_FILES:
        data = json.loads(gf.read_text())
        for c in data["cases"]:
            cases.append(pytest.param(c, id=f"{c['op']}:{c['spec']!r}"))
    return cases


def test_golden_files_present_and_sized():
    assert GOLDEN_FILES, "no golden_*.json files found under tests/spec/"
    total = sum(len(json.loads(gf.read_text())["cases"]) for gf in GOLDEN_FILES)
    assert total >= 40, f"expected >= 40 SimpleSpec golden cases, got {total}"


def _versions(version_strings):
    return [sv.Version(v) for v in version_strings]


def _run_case(case):
    op = case["op"]
    if op == "match":
        assert sv.SimpleSpec(case["spec"]).match(sv.Version(case["version"])) == case["result"]
    elif op == "invalid":
        with pytest.raises(ValueError) as exc:
            sv.SimpleSpec(case["spec"])
        assert str(exc.value) == case["error"]
    elif op == "select":
        got = sv.SimpleSpec(case["spec"]).select(_versions(case["versions"]))
        assert (str(got) if got is not None else None) == case["selected"]
    elif op == "filter":
        got = [str(v) for v in sv.SimpleSpec(case["spec"]).filter(_versions(case["versions"]))]
        assert got == case["matched"]
    else:  # pragma: no cover
        raise AssertionError(f"unknown op {op!r}")


@pytest.mark.parametrize("case", _all_cases())
def test_golden_case(case):
    _run_case(case)
