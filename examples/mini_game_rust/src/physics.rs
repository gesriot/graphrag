//! Physics rules, ported from mini_game/physics.py.

use crate::core::{Config, Obstacle, PlayerState};

pub fn apply_gravity(state: &mut PlayerState, cfg: &Config) {
    state.vy += cfg.gravity;
    state.y += state.vy;
}

pub fn apply_jump(state: &mut PlayerState, cfg: &Config) {
    state.vy = cfg.jump_velocity;
}

pub fn apply_horizontal(state: &mut PlayerState, cfg: &Config) {
    state.x += cfg.horizontal_speed;
}

pub fn is_colliding(state: &PlayerState, obs: &Obstacle) -> bool {
    if (state.x - obs.x).abs() > 0.6 {
        return false;
    }
    obs.y_low <= state.y && state.y <= obs.y_high
}

pub fn check_collisions(state: &PlayerState, cfg: &Config) -> bool {
    cfg.obstacles.iter().any(|obs| is_colliding(state, obs))
}

pub fn update_player(state: &mut PlayerState, did_jump: bool, cfg: &Config) -> bool {
    if did_jump {
        apply_jump(state, cfg);
    }
    apply_gravity(state, cfg);
    apply_horizontal(state, cfg);

    let collided_now = check_collisions(state, cfg);
    if collided_now && !state.collided {
        state.collided = true;
    }
    if !state.collided {
        state.score += 1;
    }
    // ground clamp omitted for fidelity to original (allows the same floating behavior)
    collided_now
}
