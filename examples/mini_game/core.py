"""Core data models for the mini side-scroller simulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional


@dataclass
class Config:
    """Simulation parameters (fixed for reproducibility)."""
    gravity: float = -0.8
    jump_velocity: float = 6.0
    horizontal_speed: float = 1.0
    ground_y: float = 0.0
    max_ticks: int = 50
    # Simple 1D "obstacle" model: at certain x, forbidden y interval (bottom, top)
    obstacles: List[Dict[str, float]] = field(default_factory=lambda: [
        {"x": 10.0, "y_low": -1.0, "y_high": 2.0},
        {"x": 22.0, "y_low": 1.0, "y_high": 5.0},
        {"x": 35.0, "y_low": -2.0, "y_high": 1.5},
    ])


@dataclass
class PlayerState:
    x: float = 0.0
    y: float = 0.0
    vy: float = 0.0
    score: int = 0
    collided: bool = False


@dataclass
class Event:
    """External input at a specific tick."""
    tick: int
    action: str  # "jump" or "noop"


@dataclass
class TraceRecord:
    """One frame of the simulation trace (golden record)."""
    tick: int
    x: float
    y: float
    vy: float
    score: int
    collided: bool


Trace = List[TraceRecord]


def trace_to_dicts(trace: Trace) -> List[Dict[str, Any]]:
    return [
        {
            "tick": r.tick,
            "x": r.x,
            "y": r.y,
            "vy": r.vy,
            "score": r.score,
            "collided": r.collided,
        }
        for r in trace
    ]