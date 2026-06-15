//! Simulation runner, ported from mini_game/sim.py (structure-preserving).

use crate::core::{Config, Event, PlayerState, Trace, TraceRecord};
use crate::physics::update_player;

pub fn run_simulation(
    events: &[Event],
    cfg: Option<Config>,
    initial: Option<PlayerState>,
) -> Trace {
    let cfg = cfg.unwrap_or_default();
    let mut state = initial.unwrap_or_default();

    let jump_ticks: std::collections::HashSet<usize> = events
        .iter()
        .filter(|e| e.action == "jump")
        .map(|e| e.tick)
        .collect();

    let mut trace = Vec::new();

    for tick in 0..cfg.max_ticks {
        let did_jump = jump_ticks.contains(&tick);
        update_player(&mut state, did_jump, &cfg);

        let rec = TraceRecord {
            tick,
            x: (state.x * 10000.0).round() / 10000.0,
            y: (state.y * 10000.0).round() / 10000.0,
            vy: (state.vy * 10000.0).round() / 10000.0,
            score: state.score,
            collided: state.collided,
        };
        trace.push(rec);
    }

    trace
}

pub fn events_from_jumps(jumps: &[usize]) -> Vec<Event> {
    jumps
        .iter()
        .map(|&t| Event {
            tick: t,
            action: "jump".to_string(),
        })
        .collect()
}
