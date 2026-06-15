//! Real golden contract verifier for the Rust port.
//!
//! Loads the committed Python golden_*.json files and performs frame-by-frame
//! comparison against the output of the structure-preserving simulation.
//!
//! Run with: cargo test --test golden_contract
//!
//! This makes "passes the golden contract" a hard `cargo test` failure instead of
//! a best-effort print in main.rs.

use std::fs;
use std::path::PathBuf;

use mini_game_rust::core::Config;
use mini_game_rust::sim::{events_from_jumps, run_simulation};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
struct GoldenFile {
    name: String,
    jumps: Vec<usize>,
    trace: Vec<GoldenRecord>,
}

#[derive(Debug, Deserialize, PartialEq)]
struct GoldenRecord {
    tick: usize,
    x: f64,
    y: f64,
    vy: f64,
    score: i32,
    collided: bool,
}

fn golden_path(name: &str) -> PathBuf {
    // CARGO_MANIFEST_DIR = .../graphrag/examples/mini_game_rust
    // We need .../graphrag/examples/mini_game/tests/golden_<name>.json
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // -> examples
    p.pop(); // -> graphrag root
    p.push("examples");
    p.push("mini_game");
    p.push("tests");
    p.push(format!("golden_{}.json", name));
    p
}

fn load_golden(name: &str) -> GoldenFile {
    let path = golden_path(name);
    let content = fs::read_to_string(&path)
        .unwrap_or_else(|_| panic!("Failed to read golden file: {:?}", path));
    serde_json::from_str(&content)
        .unwrap_or_else(|e| panic!("Failed to parse golden {}: {}", name, e))
}

#[test]
fn golden_contract_all_scenarios() {
    let names = [
        "no_jumps",
        "jump_once",
        "jump_to_clear_first",
        "aggressive",
        "collision_first",
    ];

    for name in names {
        let golden = load_golden(name);
        assert_eq!(golden.name, name, "golden file name mismatch for {}", name);
        let events = events_from_jumps(&golden.jumps);
        let actual = run_simulation(&events, Some(Config::default()), None);

        assert_eq!(
            actual.len(),
            golden.trace.len(),
            "Frame count mismatch for scenario {}",
            name
        );

        for (i, (a, g)) in actual.iter().zip(golden.trace.iter()).enumerate() {
            // Use same rounding discipline as the simulation
            let ax = (a.x * 10000.0).round() / 10000.0;
            let ay = (a.y * 10000.0).round() / 10000.0;
            let avy = (a.vy * 10000.0).round() / 10000.0;

            assert_eq!(a.tick, g.tick, "tick mismatch at frame {} in {}", i, name);
            assert!(
                (ax - g.x).abs() < 1e-9,
                "x mismatch at frame {} in {}: actual {} vs golden {}",
                i,
                name,
                ax,
                g.x
            );
            assert!(
                (ay - g.y).abs() < 1e-9,
                "y mismatch at frame {} in {}: actual {} vs golden {}",
                i,
                name,
                ay,
                g.y
            );
            assert!(
                (avy - g.vy).abs() < 1e-9,
                "vy mismatch at frame {} in {}: actual {} vs golden {}",
                i,
                name,
                avy,
                g.vy
            );
            assert_eq!(
                a.score, g.score,
                "score mismatch at frame {} in {}",
                i, name
            );
            assert_eq!(
                a.collided, g.collided,
                "collided mismatch at frame {} in {}",
                i, name
            );
        }
    }
}

#[test]
fn collision_first_specific() {
    let golden = load_golden("collision_first");
    let events = events_from_jumps(&golden.jumps);
    let trace = run_simulation(&events, Some(Config::default()), None);

    let first_collision = trace.iter().find(|r| r.collided);
    assert!(
        first_collision.is_some(),
        "Expected at least one collision for collision_first"
    );
    let fc = first_collision.unwrap();
    assert_eq!(fc.tick, 9, "collision_first must collide exactly at tick 9");
    assert!(
        (fc.x - 10.0).abs() < 0.7,
        "collision should be near first obstacle x=10"
    );
}
