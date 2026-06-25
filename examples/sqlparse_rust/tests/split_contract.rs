//! Golden contract verifier for the Rust `sqlparse.split` pipeline (Stage 4).
//! Reads examples/sqlparse/tests/split/golden_*.json and asserts parity.
//! Run with: cargo test --test split_contract

use std::fs;
use std::path::PathBuf;

use serde_json::Value;
use sqlparse_rust::split::split;

fn split_golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop();
    p.push("sqlparse");
    p.push("tests");
    p.push("split");
    p
}

#[test]
fn split_contract_all_cases() {
    let mut files: Vec<PathBuf> = fs::read_dir(split_golden_dir())
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
        for case in data["cases"].as_array().unwrap() {
            let sql = case["sql"].as_str().unwrap();
            let strip = case["strip_semicolon"].as_bool().unwrap();
            let got: Vec<String> = split(sql, strip);
            let want: Vec<String> = case["result"]
                .as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_string())
                .collect();
            assert_eq!(got, want, "split mismatch for {:?} (strip={})", sql, strip);
            total += 1;
        }
    }
    assert!(total >= 25, "expected >= 25 split cases, got {}", total);
}
