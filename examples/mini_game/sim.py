"""Simulation runner. Pure, deterministic, produces golden traces."""

from __future__ import annotations

from typing import List, Iterable, Dict, Any

from .core import Config, PlayerState, Event, Trace, TraceRecord
from .physics import update_player


def run_simulation(
    events: Iterable[Event],
    cfg: Config | None = None,
    initial: PlayerState | None = None,
) -> Trace:
    """Run the side-scroller simulation for a sequence of events.

    Returns a complete trace suitable for golden-master testing and behavior contracts.
    """
    cfg = cfg or Config()
    state = initial or PlayerState()

    # Index events by tick for O(1) lookup
    jump_ticks: set[int] = {e.tick for e in events if e.action == "jump"}

    trace: Trace = []

    for tick in range(cfg.max_ticks):
        did_jump = tick in jump_ticks
        update_player(state, did_jump=did_jump, cfg=cfg)

        rec = TraceRecord(
            tick=tick,
            x=round(state.x, 4),
            y=round(state.y, 4),
            vy=round(state.vy, 4),
            score=state.score,
            collided=state.collided,
        )
        trace.append(rec)

        if state.collided and tick > 5:  # allow a few steps after first collision for traces
            # continue to max_ticks so full behavior is captured
            pass

    return trace


def events_from_list(jump_ticks: List[int]) -> List[Event]:
    return [Event(tick=t, action="jump") for t in jump_ticks]


# Pre-defined input sequences for golden traces
GOLDEN_INPUTS: Dict[str, List[int]] = {
    "no_jumps": [],
    "jump_once": [3],
    "jump_to_clear_first": [3, 8],
    "aggressive": [1, 5, 12, 20, 28],
}
