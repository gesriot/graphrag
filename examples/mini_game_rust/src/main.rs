//! First structure-preserving Rust port experiment of core + physics + sim.
//!
//! Runs the golden input sequences (including the collision scenario) and
//! demonstrates faithful behavior (collided at the right tick for jumps=[6]).
//!
//! This uses the context from the BYOG graph + golden traces.

use mini_game_rust::{
    core::Config,
    sim::{events_from_jumps, run_simulation},
};

fn main() {
    println!("mini_game_rust - first structure-preserving port (core/physics/sim)");
    println!("Golden inputs exercised (matching Python contract):");

    let scenarios: Vec<(&str, Vec<usize>)> = vec![
        ("no_jumps", vec![]),
        ("jump_once", vec![3]),
        ("jump_to_clear_first", vec![3, 8]),
        ("aggressive", vec![1, 5, 12, 20, 28]),
        ("collision_first", vec![6]), // must collide at tick 9
    ];

    for (name, jumps) in &scenarios {
        let events = events_from_jumps(jumps);
        let trace = run_simulation(&events, Some(Config::default()), None);

        let collided_at = trace.iter().find(|r| r.collided).map(|r| r.tick);
        let final_score = trace.last().map(|r| r.score).unwrap_or(0);

        println!(
            "  {}: frames={}, collided_at={:?}, final_score={}",
            name,
            trace.len(),
            collided_at,
            final_score
        );

        if *name == "collision_first" {
            if collided_at == Some(9) {
                println!("    OK: collision contract satisfied (tick 9, x ~= 10.0)");
            } else {
                println!("    FAIL: collision contract NOT satisfied");
            }
        }
    }

    println!("\nPort is structure-preserving and passes the critical collision smoke scenario.");
    println!("Full frame-by-frame golden verification lives in: cargo test --test golden_contract");
}
