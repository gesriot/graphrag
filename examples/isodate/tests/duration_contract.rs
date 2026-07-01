//! Hidden Rust golden contract for the isodate parse_duration ablation.
//! Injected by ablation.py eval into a filled-in kit (crate `arm`), golden dir
//! patched. Reads examples/isodate/tests/duration/golden_*.json (Python oracle)
//! and asserts arm::parse_duration(input) matches the descriptor (kind + fields).

use std::fs;
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
    for path in &files {
        let data: Value = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
        for c in data["cases"].as_array().unwrap() {
            let input = c["input"].as_str().unwrap();
            let kind = c["kind"].as_str().unwrap();
            match arm::parse_duration(input) {
                Err(_) => assert_eq!(kind, "error", "expected non-error for {input:?}"),
                Ok(arm::Parsed::Timedelta { days, seconds, microseconds }) => {
                    assert_eq!(kind, "timedelta", "kind for {input:?}");
                    assert_eq!(c["years"].as_f64().unwrap(), 0.0, "{input:?}");
                    assert_eq!(c["months"].as_f64().unwrap(), 0.0, "{input:?}");
                    assert_eq!(days, c["days"].as_i64().unwrap(), "days {input:?}");
                    assert_eq!(seconds, c["seconds"].as_i64().unwrap(), "seconds {input:?}");
                    assert_eq!(microseconds, c["microseconds"].as_i64().unwrap(), "micros {input:?}");
                }
                Ok(arm::Parsed::Duration { years, months, days, seconds, microseconds }) => {
                    assert_eq!(kind, "duration", "kind for {input:?}");
                    assert_eq!(years, c["years"].as_f64().unwrap(), "years {input:?}");
                    assert_eq!(months, c["months"].as_f64().unwrap(), "months {input:?}");
                    assert_eq!(days, c["days"].as_i64().unwrap(), "days {input:?}");
                    assert_eq!(seconds, c["seconds"].as_i64().unwrap(), "seconds {input:?}");
                    assert_eq!(microseconds, c["microseconds"].as_i64().unwrap(), "micros {input:?}");
                }
            }
            total += 1;
        }
    }
    assert!(total >= 20, "expected >= 20 cases, got {total}");
}
