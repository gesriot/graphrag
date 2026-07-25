//! Hidden Rust golden contract for the isodate parse_duration ablation.
//! Injected by ablation.py eval into a filled-in kit (crate `arm`), golden dir
//! patched. Reads examples/isodate/tests/duration/golden_*.json (Python oracle)
//! and asserts arm::parse_duration(input) matches the descriptor (kind + fields).
//!
//! Per-case outcomes are collected (not aborted on first mismatch) and printed as
//! one machine-readable ABLATION_SCORE line for `ablation.py eval`. An aggregate
//! assertion still fails the test so `cargo test` on a correct port stays gated.

use std::fs;
use std::panic::{self, AssertUnwindSafe};
use std::path::PathBuf;

use serde_json::Value;

fn duration_golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop();
    p.push("isodate");
    p.push("tests");
    p.push("duration");
    p
}

/// Check one golden case. Ok on match; Err(case_id) on mismatch. Case id = input.
fn check_case(c: &Value) -> Result<(), String> {
    let input = c["input"].as_str().unwrap().to_string();
    let kind = c["kind"].as_str().unwrap();
    match arm::parse_duration(&input) {
        Err(_) => {
            if kind != "error" {
                return Err(input);
            }
        }
        Ok(arm::Parsed::Timedelta {
            days,
            seconds,
            microseconds,
        }) => {
            if kind != "timedelta"
                || c["years"].as_f64().unwrap() != 0.0
                || c["months"].as_f64().unwrap() != 0.0
                || days != c["days"].as_i64().unwrap()
                || seconds != c["seconds"].as_i64().unwrap()
                || microseconds != c["microseconds"].as_i64().unwrap()
            {
                return Err(input);
            }
        }
        Ok(arm::Parsed::Duration {
            years,
            months,
            days,
            seconds,
            microseconds,
        }) => {
            if kind != "duration"
                || years != c["years"].as_f64().unwrap()
                || months != c["months"].as_f64().unwrap()
                || days != c["days"].as_i64().unwrap()
                || seconds != c["seconds"].as_i64().unwrap()
                || microseconds != c["microseconds"].as_i64().unwrap()
            {
                return Err(input);
            }
        }
    }
    Ok(())
}

#[test]
fn isodate_duration_contract() {
    let mut files: Vec<PathBuf> = fs::read_dir(duration_golden_dir())
        .unwrap()
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with("golden_") && n.ends_with(".json"))
                .unwrap_or(false)
        })
        .collect();
    files.sort();
    assert!(!files.is_empty());

    let mut total = 0usize;
    let mut passed = 0usize;
    let mut failed: Vec<String> = Vec::new();
    // A panic in the port under test must cost one case, not the whole score:
    // an unwinding `unwrap` on a single malformed input would otherwise abort
    // before the summary line and report a partial port as 0/N. The hook is
    // silenced so panic spew cannot interleave with the parsed output.
    let prev_hook = panic::take_hook();
    panic::set_hook(Box::new(|_| {}));
    for path in &files {
        let data: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
        for c in data["cases"].as_array().unwrap() {
            total += 1;
            let id = c["input"].as_str().unwrap_or("<missing input>").to_string();
            match panic::catch_unwind(AssertUnwindSafe(|| check_case(c))) {
                Ok(Ok(())) => passed += 1,
                Ok(Err(id)) => failed.push(id),
                Err(_) => failed.push(format!("{id} (panic)")),
            }
        }
    }
    panic::set_hook(prev_hook);

    // One-line machine-readable summary for ablation.py eval (-- --nocapture).
    // Shape is stable: ABLATION_SCORE {"passed": N, "total": M, "failed": ["...", ...]}
    let summary = serde_json::json!({
        "passed": passed,
        "total": total,
        "failed": failed,
    });
    println!("ABLATION_SCORE {}", summary);

    assert!(total >= 20, "expected >= 20 cases, got {total}");
    assert!(
        failed.is_empty(),
        "contract failed {}/{} cases: {failed:?}",
        failed.len(),
        total
    );
}
