"""Golden-trace tests for the deterministic mini side-scroller.

These tests define the *behavior contract* for any port (Python->Rust etc.).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from examples.mini_game.sim import run_simulation, GOLDEN_INPUTS, events_from_list


TESTS_DIR = Path(__file__).parent
GOLDEN_DIR = TESTS_DIR


def load_golden(name: str):
    path = GOLDEN_DIR / f"golden_{name}.json"
    data = json.loads(path.read_text())
    return data["trace"]


@pytest.mark.parametrize("name", list(GOLDEN_INPUTS.keys()))
def test_golden_trace_matches(name: str):
    """The simulation must produce exactly the same trace as the committed golden file."""
    jumps = GOLDEN_INPUTS[name]
    events = events_from_list(jumps)
    actual = run_simulation(events)
    expected = load_golden(name)

    assert len(actual) == len(expected), f"Frame count mismatch for {name}"

    for i, (a, e) in enumerate(zip(actual, expected)):
        assert a.tick == e["tick"]
        assert abs(a.x - e["x"]) < 1e-6, f"x mismatch at tick {i}"
        assert abs(a.y - e["y"]) < 1e-6, f"y mismatch at tick {i}"
        assert abs(a.vy - e["vy"]) < 1e-6, f"vy mismatch at tick {i}"
        assert a.score == e["score"]
        assert a.collided == e["collided"]


def test_no_collision_on_safe_path():
    """With correct jumps the first obstacle should be cleared without collision."""
    jumps = GOLDEN_INPUTS["jump_to_clear_first"]
    events = events_from_list(jumps)
    trace = run_simulation(events)
    # At the x of first obstacle (~10), we should not have collided
    for rec in trace:
        if rec.x >= 9.5 and rec.x <= 10.5:
            assert not rec.collided, "Collided on the 'jump_to_clear_first' golden path"
            break
    else:
        pytest.fail("Never reached first obstacle x in trace")


if __name__ == "__main__":
    # Convenience: python -m examples.mini_game.tests.test_sim  (adjust path if needed)
    pytest.main([__file__, "-q"])
