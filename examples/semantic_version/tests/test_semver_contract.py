"""Golden-master behavior contract for the vendored `semantic_version.Version`
(third Python->Rust porting target, v1 scope = Version only).

Each golden_*.json case pins one operation against the reference Python:
- parse:   string -> (major, minor, patch, prerelease, build, str)
- compare: (a, b) -> -1 / 0 / 1 / "incomparable"   (build metadata is not ordered)
- eq:      (a, b) -> bool                            (exact, build-sensitive)
- invalid: string -> ValueError message
- coerce:  string -> normalized str

The Rust port must reproduce these exactly.

Run:
    uv run python -m pytest examples/semantic_version/tests -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parents[2]
sys.path.insert(0, str(EXAMPLES_DIR))

import semantic_version as sv  # type: ignore  # noqa: E402

PKG_DIR = Path(__file__).parent.parent
GOLDEN_FILES = sorted(PKG_DIR.glob("tests/golden_*.json"))


def _all_cases():
    cases = []
    for gf in GOLDEN_FILES:
        data = json.loads(gf.read_text())
        for c in data["cases"]:
            label = c.get("input") or f"{c.get('a')}|{c.get('b')}"
            cases.append(pytest.param(c, id=f"{c['op']}:{label}"))
    return cases


def test_golden_files_present_and_sized():
    assert GOLDEN_FILES, "no golden_*.json files found"
    total = sum(len(json.loads(gf.read_text())["cases"]) for gf in GOLDEN_FILES)
    assert total >= 30, f"expected >= 30 golden cases, got {total}"


def _run_case(case):
    op = case["op"]
    if op == "parse":
        v = sv.Version(case["input"])
        assert v.major == case["major"]
        assert v.minor == case["minor"]
        assert v.patch == case["patch"]
        assert list(v.prerelease) == case["prerelease"]
        assert list(v.build) == case["build"]
        assert str(v) == case["str"]
    elif op == "compare":
        r = sv.compare(case["a"], case["b"])
        got = "incomparable" if r is NotImplemented else r
        assert got == case["result"]
    elif op == "eq":
        assert (sv.Version(case["a"]) == sv.Version(case["b"])) == case["result"]
    elif op == "invalid":
        with pytest.raises(ValueError) as exc:
            sv.Version(case["input"])
        assert str(exc.value) == case["error"]
    elif op == "coerce":
        assert str(sv.Version.coerce(case["input"])) == case["str"]
    else:  # pragma: no cover
        raise AssertionError(f"unknown op {op!r}")


@pytest.mark.parametrize("case", _all_cases())
def test_golden_case(case):
    _run_case(case)
