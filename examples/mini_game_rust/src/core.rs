//! Core types, ported structure-preservingly from Python mini_game/core.py + dataclasses.

#[derive(Clone, Debug)]
pub struct Config {
    pub gravity: f64,
    pub jump_velocity: f64,
    pub horizontal_speed: f64,
    pub ground_y: f64,
    pub max_ticks: usize,
    pub obstacles: Vec<Obstacle>,
}

#[derive(Clone, Debug)]
pub struct Obstacle {
    pub x: f64,
    pub y_low: f64,
    pub y_high: f64,
}

impl Default for Config {
    fn default() -> Self {
        Self {
            gravity: -0.8,
            jump_velocity: 6.0,
            horizontal_speed: 1.0,
            ground_y: 0.0,
            max_ticks: 50,
            obstacles: vec![
                Obstacle {
                    x: 10.0,
                    y_low: -1.0,
                    y_high: 2.0,
                },
                Obstacle {
                    x: 22.0,
                    y_low: 1.0,
                    y_high: 5.0,
                },
                Obstacle {
                    x: 35.0,
                    y_low: -2.0,
                    y_high: 1.5,
                },
            ],
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct PlayerState {
    pub x: f64,
    pub y: f64,
    pub vy: f64,
    pub score: i32,
    pub collided: bool,
}

#[derive(Clone, Debug)]
pub struct Event {
    pub tick: usize,
    pub action: String, // "jump" or "noop"
}

#[derive(Clone, Debug)]
pub struct TraceRecord {
    pub tick: usize,
    pub x: f64,
    pub y: f64,
    pub vy: f64,
    pub score: i32,
    pub collided: bool,
}

pub type Trace = Vec<TraceRecord>;
