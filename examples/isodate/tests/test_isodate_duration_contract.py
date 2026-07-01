"""Golden contract for the vendored isodate `parse_duration` slice
(Phase 7 ablation v3 target).

Ground truth is the vendored Python `isodate.parse_duration`, which returns a
`datetime.timedelta` (fixed-only ISO strings) or an `isodate.Duration`
(years/months present). Each case pins an explicit oracle **descriptor**
(kind + years/months/days/seconds/microseconds/total_seconds), never a blind
string round-trip, so it measures parser parity rather than formatter
normalization (see PHASE7_ISODATE_V3_PREREG.md, design decision 2).

Re-derives the descriptor from the vendored library to keep the golden in sync.

Run: uv run python -m pytest examples/isodate/tests/test_isodate_duration_contract.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
EXAMPLES = HERE.parents[1]  # .../examples
GOLDEN = HERE / "duration" / "golden_duration.json"
sys.path.insert(0, str(EXAMPLES))


def _descr(inp: str) -> dict:
    import isodate
    from isodate.duration import Duration

    try:
        r = isodate.parse_duration(inp)
    except Exception:
        return {"input": inp, "kind": "error"}
    if isinstance(r, Duration):
        return {
            "input": inp, "kind": "duration",
            "years": float(r.years), "months": float(r.months),
            "days": r.tdelta.days, "seconds": r.tdelta.seconds,
            "microseconds": r.tdelta.microseconds, "total_seconds": None,
        }
    return {
        "input": inp, "kind": "timedelta", "years": 0.0, "months": 0.0,
        "days": r.days, "seconds": r.seconds, "microseconds": r.microseconds,
        "total_seconds": r.total_seconds(),
    }


def test_golden_present_and_sized():
    cases = json.loads(GOLDEN.read_text())["cases"]
    assert len(cases) >= 20
    kinds = {c["kind"] for c in cases}
    assert {"timedelta", "duration", "error"} <= kinds


def test_isodate_duration_golden_matches_reference():
    cases = json.loads(GOLDEN.read_text())["cases"]
    for c in cases:
        got = _descr(c["input"])
        assert got == c, f"descriptor drift for {c['input']!r}: {got} != {c}"
