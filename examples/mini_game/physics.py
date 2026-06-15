"""Deterministic physics and collision rules for the mini side-scroller."""

from __future__ import annotations

from typing import List, Dict, Tuple

from .core import PlayerState, Config


def apply_gravity(state: PlayerState, cfg: Config) -> None:
    state.vy += cfg.gravity
    state.y += state.vy


def apply_jump(state: PlayerState, cfg: Config) -> None:
    state.vy = cfg.jump_velocity


def apply_horizontal(state: PlayerState, cfg: Config) -> None:
    state.x += cfg.horizontal_speed


def is_colliding(state: PlayerState, obstacle: Dict[str, float]) -> bool:
    """Check if player at current x is inside the forbidden y-range of obstacle."""
    if abs(state.x - obstacle["x"]) > 0.6:  # tolerance for discrete ticks
        return False
    return obstacle["y_low"] <= state.y <= obstacle["y_high"]


def check_collisions(state: PlayerState, cfg: Config) -> bool:
    for obs in cfg.obstacles:
        if is_colliding(state, obs):
            return True
    return False


def update_player(state: PlayerState, did_jump: bool, cfg: Config) -> bool:
    """Advance one physics step. Returns whether a new collision occurred this step."""
    if did_jump:
        apply_jump(state, cfg)

    apply_gravity(state, cfg)
    apply_horizontal(state, cfg)

    collided_now = check_collisions(state, cfg)
    if collided_now and not state.collided:
        state.collided = True

    # Simple scoring: progress + survival
    if not state.collided:
        state.score += 1

    # Clamp to "ground" for nicer traces (not physical bounce)
    if state.y < cfg.ground_y and state.vy < 0:
        # allow floating but record realistic
        pass

    return collided_now


def find_obstacle_at_x(x: float, cfg: Config) -> dict[str, float] | None:
    for obs in cfg.obstacles:
        if abs(x - obs["x"]) < 0.7:
            return obs
    return None
