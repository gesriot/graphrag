//! Golden contract verifier for the Rust port of `semantic_version.Version`.
//!
//! Loads the committed Python golden_*.json (single source of truth) and asserts
//! the Rust port reproduces every case: parse / compare / eq / invalid / coerce.
//!
//! Run with: cargo test --test golden_contract

use std::fs;
use std::path::PathBuf;

use semantic_version_rust::{compare, Version};
use serde_json::Value;

fn golden_dir() -> PathBuf {
    // CARGO_MANIFEST_DIR = .../graphrag/examples/semantic_version_rust
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.pop(); // -> examples
    p.push("semantic_version");
    p.push("tests");
    p
}

fn str_vec(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_string())
        .collect()
}

fn run_case(c: &Value) {
    match c["op"].as_str().unwrap() {
        "parse" => {
            let v = Version::parse(c["input"].as_str().unwrap()).expect("parse should succeed");
            assert_eq!(v.major, c["major"].as_u64().unwrap());
            assert_eq!(v.minor, c["minor"].as_u64().unwrap());
            assert_eq!(v.patch, c["patch"].as_u64().unwrap());
            assert_eq!(v.prerelease, str_vec(&c["prerelease"]));
            assert_eq!(v.build, str_vec(&c["build"]));
            assert_eq!(v.to_string(), c["str"].as_str().unwrap());
        }
        "compare" => {
            let got = compare(c["a"].as_str().unwrap(), c["b"].as_str().unwrap());
            let got_value = match got {
                None => Value::String("incomparable".to_string()),
                Some(n) => Value::from(n),
            };
            assert_eq!(got_value, c["result"], "compare {} vs {}", c["a"], c["b"]);
        }
        "eq" => {
            let a = Version::parse(c["a"].as_str().unwrap()).unwrap();
            let b = Version::parse(c["b"].as_str().unwrap()).unwrap();
            assert_eq!((a == b), c["result"].as_bool().unwrap());
        }
        "invalid" => {
            let err =
                Version::parse(c["input"].as_str().unwrap()).expect_err("invalid input must fail");
            assert_eq!(err, c["error"].as_str().unwrap());
        }
        "coerce" => {
            let v = Version::coerce(c["input"].as_str().unwrap()).expect("coerce should succeed");
            assert_eq!(v.to_string(), c["str"].as_str().unwrap());
        }
        other => panic!("unknown op {other}"),
    }
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
    assert!(total >= 30, "expected >= 30 golden cases, got {}", total);
}
