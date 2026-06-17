//! Golden contract verifier for the Rust match (v2 / Bitap) port.
//!
//! Loads examples/diff_match_patch/tests/match/golden_*.json and asserts the Rust
//! port reproduces `match_main(text, pattern, loc)` exactly, including patterns
//! longer than 64/128 chars (which require the arbitrary-precision bit arrays).
//!
//! Run with: cargo test --test match_contract

use std::fs;
use std::path::PathBuf;

use diff_match_patch_rust::DiffMatchPatch;
use serde_json::Value;

fn match_golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // -> examples
    p.push("diff_match_patch");
    p.push("tests");
    p.push("match");
    p
}

fn run_case(c: &Value) {
    assert_eq!(c["op"].as_str().unwrap(), "match");
    let mut dmp = DiffMatchPatch::new();
    dmp.match_threshold = c["match_threshold"].as_f64().unwrap();
    dmp.match_distance = c["match_distance"].as_i64().unwrap();
    let got = dmp.match_main(
        c["text"].as_str().unwrap(),
        c["pattern"].as_str().unwrap(),
        c["loc"].as_i64().unwrap(),
    );
    assert_eq!(
        got,
        c["result"].as_i64().unwrap(),
        "match text={:?} pattern={:?} loc={}",
        c["text"],
        c["pattern"],
        c["loc"]
    );
}

#[test]
fn match_contract_all_cases() {
    let dir = match_golden_dir();
    let mut files: Vec<PathBuf> = fs::read_dir(&dir)
        .unwrap_or_else(|_| panic!("cannot read match golden dir {:?}", dir))
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with("golden_") && n.ends_with(".json"))
                .unwrap_or(false)
        })
        .collect();
    files.sort();
    assert!(!files.is_empty(), "no golden_*.json in {:?}", dir);

    let mut total = 0usize;
    for path in &files {
        let content = fs::read_to_string(path).unwrap_or_else(|_| panic!("cannot read {:?}", path));
        let data: Value = serde_json::from_str(&content).unwrap();
        for case in data["cases"].as_array().unwrap() {
            run_case(case);
            total += 1;
        }
    }
    assert!(
        total >= 25,
        "expected >= 25 match golden cases, got {}",
        total
    );
}
