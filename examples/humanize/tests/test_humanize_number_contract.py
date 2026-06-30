"""Golden contract for the vendored humanize number-formatting slice
(Phase 7 ablation v2 target).

Ground truth is the vendored Python ``humanize.number`` (default locale), which
depends cross-module on ``humanize.i18n``. Each case pins ``func(*args) -> str``.
Scope = the bounded number formatters only:
``intcomma / intword / apnumber / ordinal / fractional / scientific``.

This re-derives the contract from the vendored library to keep the golden in
sync. time/filesize/lists and locale-catalog loading are out of scope.

Run: uv run python -m pytest examples/humanize/tests/test_humanize_number_contract.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
EXAMPLES = HERE.parents[1]  # .../examples
GOLDEN = HERE / "number" / "golden_number.json"
sys.path.insert(0, str(EXAMPLES))

IN_SCOPE = {"intcomma", "intword", "apnumber", "ordinal", "fractional", "scientific"}


def test_golden_present_and_sized():
    cases = json.loads(GOLDEN.read_text())["cases"]
    assert len(cases) >= 50
    assert {c["func"] for c in cases} == IN_SCOPE


def test_humanize_number_golden_matches_reference():
    from humanize import number  # vendored; imports vendored humanize.i18n

    cases = json.loads(GOLDEN.read_text())["cases"]
    for c in cases:
        fn = getattr(number, c["func"])
        if "error" in c:
            continue
        got = fn(*c["args"])
        assert got == c["result"], f"{c['func']}{tuple(c['args'])}: {got!r} != {c['result']!r}"
