//! Golden contract verifier for the Rust port.
//!
//! Loads the committed Python golden_*.json files (the single source of truth)
//! and asserts `run_source(source) == (stdout, error)` for every case. This makes
//! "the port matches Python" a hard `cargo test` failure, not a best-effort print.
//!
//! Run with: cargo test --test golden_contract

use std::fs;
use std::path::PathBuf;

use mini_lang_rust::run_source;
use serde::Deserialize;

#[derive(Debug, Deserialize)]
struct GoldenFile {
    #[allow(dead_code)]
    name: String,
    cases: Vec<GoldenCase>,
}

#[derive(Debug, Deserialize)]
struct GoldenCase {
    source: String,
    stdout: Vec<String>,
    error: Option<String>,
}

fn golden_dir() -> PathBuf {
    // CARGO_MANIFEST_DIR = .../graphrag/examples/mini_lang_rust
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // -> examples
    p.push("mini_lang");
    p.push("tests");
    p
}

#[test]
fn golden_contract_all_cases() {
    let dir = golden_dir();
    let mut files: Vec<PathBuf> = fs::read_dir(&dir)
        .unwrap_or_else(|_| panic!("cannot read golden dir {:?}", dir))
        .filter_map(|e| e.ok().map(|e| e.path()))
        .filter(|p| {
            p.file_name()
                .and_then(|n| n.to_str())
                .map(|n| n.starts_with("golden_") && n.ends_with(".json"))
                .unwrap_or(false)
        })
        .collect();
    files.sort();
    assert!(!files.is_empty(), "no golden_*.json files in {:?}", dir);

    let mut total = 0usize;
    for path in &files {
        let content = fs::read_to_string(path).unwrap_or_else(|_| panic!("cannot read {:?}", path));
        let gf: GoldenFile = serde_json::from_str(&content)
            .unwrap_or_else(|e| panic!("cannot parse {:?}: {}", path, e));
        for case in &gf.cases {
            let (out, err) = run_source(&case.source);
            assert_eq!(out, case.stdout, "stdout mismatch for {:?}", case.source);
            assert_eq!(err, case.error, "error mismatch for {:?}", case.source);
            total += 1;
        }
    }
    assert!(total >= 20, "expected >= 20 golden cases, got {}", total);
}
