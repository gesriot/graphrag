//! Golden contract verifier for the Rust diff (v1) port of diff-match-patch.
//!
//! Loads examples/diff_match_patch/tests/diff/golden_*.json and asserts the Rust
//! port reproduces every case: diff / cleanup_semantic / cleanup_efficiency /
//! cleanup_merge.
//!
//! Run with: cargo test --test diff_contract

use std::fs;
use std::path::PathBuf;

use diff_match_patch_rust::DiffMatchPatch;
use serde_json::Value;

fn diff_golden_dir() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // -> examples
    p.push("diff_match_patch");
    p.push("tests");
    p.push("diff");
    p
}

fn to_ops(v: &Value) -> Vec<(i32, String)> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|pair| {
            let a = pair.as_array().unwrap();
            (
                a[0].as_i64().unwrap() as i32,
                a[1].as_str().unwrap().to_string(),
            )
        })
        .collect()
}

fn ops_to_json(diffs: &[(i32, String)]) -> Value {
    Value::Array(
        diffs
            .iter()
            .map(|(op, t)| Value::Array(vec![Value::from(*op), Value::from(t.clone())]))
            .collect(),
    )
}

fn run_case(c: &Value) {
    let dmp = DiffMatchPatch::new();
    match c["op"].as_str().unwrap() {
        "diff" => {
            let diffs = dmp.diff_main(
                c["text1"].as_str().unwrap(),
                c["text2"].as_str().unwrap(),
                c["checklines"].as_bool().unwrap(),
            );
            assert_eq!(ops_to_json(&diffs), c["diffs"], "diff case {:?}", c);
        }
        "cleanup_semantic" => {
            let mut diffs = to_ops(&c["input"]);
            dmp.diff_cleanup_semantic(&mut diffs);
            assert_eq!(
                ops_to_json(&diffs),
                c["expected"],
                "semantic {:?}",
                c["input"]
            );
        }
        "cleanup_efficiency" => {
            let mut diffs = to_ops(&c["input"]);
            dmp.diff_cleanup_efficiency(&mut diffs); // edit_cost defaults to 4
            assert_eq!(
                ops_to_json(&diffs),
                c["expected"],
                "efficiency {:?}",
                c["input"]
            );
        }
        "cleanup_merge" => {
            let mut diffs = to_ops(&c["input"]);
            dmp.diff_cleanup_merge(&mut diffs);
            assert_eq!(ops_to_json(&diffs), c["expected"], "merge {:?}", c["input"]);
        }
        other => panic!("unknown op {other}"),
    }
}

#[test]
fn diff_contract_all_cases() {
    let dir = diff_golden_dir();
    let mut files: Vec<PathBuf> = fs::read_dir(&dir)
        .unwrap_or_else(|_| panic!("cannot read diff golden dir {:?}", dir))
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
        total >= 35,
        "expected >= 35 diff golden cases, got {}",
        total
    );
}
